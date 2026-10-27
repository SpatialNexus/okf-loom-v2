/* portable-feedback.js: offline page-level feedback notes (roadmap X2).
 *
 * Emitted ONLY into static and single-file exports built with
 * --include-portable-feedback. A reader writes one note about the page on
 * screen and downloads it as a file. Nothing is sent anywhere: this script
 * makes no network request, keeps the draft in memory only (no Web Storage,
 * IndexedDB, cookies or URL state) and carries no Studio address or
 * credential. A local Studio operator may later import the file. The server
 * parser (scripts/okf_loom/portable_feedback.py) is authoritative; the limits
 * below mirror it so a note that downloads here also parses there.
 *
 * Every string reaches the DOM through textContent or setAttribute.
 */
(function () {
  "use strict";
  if (window.__okfPortableFeedbackMounted) return;
  window.__okfPortableFeedbackMounted = true;

  var FORMAT = "okf-loom.portable-feedback";
  var NOTE_VERSION = 1;
  var BOOTSTRAP_VERSION = 1;
  var MAX_NOTE_BYTES = 32768;
  var MAX_BODY_CODE_POINTS = 8000;
  var MAX_QUOTE_CODE_POINTS = 1000;
  var MAX_CLAIMED_AUTHOR_CODE_POINTS = 80;
  var BOOTSTRAP_ID = "okf-portable-feedback";
  var SHA256 = /^[0-9a-f]{64}$/;
  // Python str.strip() whitespace: the body needs one character outside it.
  var NON_WHITESPACE = /[^\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/;
  // Body and quote: C0 controls other than TAB and LF.
  var TEXT_CONTROL = /[\x00-\x08\x0b-\x1f]/;
  // Claimed name: C0, DEL, C1 and bidi embedding/override/isolate controls.
  var LABEL_CONTROL = /[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/;
  var LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?:^|[^\ud800-\udbff])[\udc00-\udfff]/;
  var UNSAVED = "Your note hasn't been downloaded and will be lost if you leave.";

  function codePoints(text) { return Array.from(text).length; }
  function wellFormed(text) {
    return typeof text.isWellFormed === "function" ? text.isWellFormed() : !LONE_SURROGATE.test(text);
  }
  function count(n) { return Number(n).toLocaleString("en-US"); }
  function own(map, key) { return Object.prototype.hasOwnProperty.call(map, key); }

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
  function setHidden(node, hidden) { if (node.hidden !== hidden) node.hidden = hidden; }
  function setAttr(node, name, value) {
    if (value === null) { if (node.hasAttribute(name)) node.removeAttribute(name); }
    else if (node.getAttribute(name) !== value) node.setAttribute(name, value);
  }

  // ---- Bootstrap: non-executable JSON written by the exporter ------------
  function admitTarget(value) {
    if (!value || typeof value !== "object") return null;
    var documentPath = value.document, sha = value.source_sha256;
    if (typeof documentPath !== "string" || typeof sha !== "string" || !SHA256.test(sha)) return null;
    if (documentPath.length > 1024 || !/\.md$/.test(documentPath) || /^\/|\\|[\x00-\x1f]/.test(documentPath)) return null;
    var parts = documentPath.split("/");
    for (var i = 0; i < parts.length; i++) if (!parts[i] || parts[i] === "." || parts[i] === "..") return null;
    var leaf = parts[parts.length - 1];
    if (leaf === "index.md" || leaf === "log.md") return null;
    return { document: documentPath, sha: sha, concept: documentPath.slice(0, -3) };
  }
  function readBootstrap() {
    var node = document.getElementById(BOOTSTRAP_ID);
    if (!node) return null;
    var data;
    try { data = JSON.parse(node.textContent || ""); } catch (e) { return null; }
    if (!data || typeof data !== "object" || data.version !== BOOTSTRAP_VERSION) return null;
    if (own(data, "documents")) {
      if (!data.documents || typeof data.documents !== "object") return null;
      var map = Object.create(null);
      Object.keys(data.documents).forEach(function (conceptId) {
        var target = admitTarget(data.documents[conceptId]);
        if (target && target.concept === conceptId) map[conceptId] = target;
      });
      return { kind: "map", documents: map };
    }
    var one = admitTarget(data);
    return one ? { kind: "page", target: one } : null;
  }

  // ---- Drafts: memory only, one per target document ----------------------
  var drafts = Object.create(null);
  var current = null;          // the draft the open composer edits
  var closeIntent = null;      // "done" | "discard"; null means the browser closed it
  var copyGeneration = 0;
  var launchers = [];
  var ui = null;

  function draftFor(target) {
    if (!own(drafts, target.document)) {
      drafts[target.document] = { target: target, body: "", quote: "", name: "", time: false, last: null, savedKey: null };
    }
    return drafts[target.document];
  }
  function keyOf(draft) { return JSON.stringify([draft.body, draft.quote, draft.name, !!draft.time]); }
  function hasContent(draft) { return !!(draft.body || draft.quote || draft.name); }
  function isDirty(draft) { return hasContent(draft) && draft.savedKey !== keyOf(draft); }
  function anyDirty() {
    for (var documentPath in drafts) if (isDirty(drafts[documentPath])) return true;
    return false;
  }

  var guarded = false;
  function onBeforeUnload(event) {
    if (!anyDirty()) return undefined;
    event.preventDefault();
    event.returnValue = "";
    return "";
  }
  function syncUnloadGuard() {
    var need = anyDirty();
    if (need === guarded) return;
    guarded = need;
    if (need) window.addEventListener("beforeunload", onBeforeUnload);
    else window.removeEventListener("beforeunload", onBeforeUnload);
  }

  // ---- Note construction (mirrors portable_feedback.parse_note) ----------
  function problemsOf(draft) {
    var found = [];
    var body = draft.body;
    if (!body || !NON_WHITESPACE.test(body)) found.push(["body", "Write your note before downloading."]);
    else if (!wellFormed(body)) found.push(["body", "Remove the unsupported character from the note."]);
    else if (codePoints(body) > MAX_BODY_CODE_POINTS) found.push(["body", "Shorten the note to " + count(MAX_BODY_CODE_POINTS) + " characters or fewer (it has " + count(codePoints(body)) + ")."]);
    else if (TEXT_CONTROL.test(body)) found.push(["body", "Remove control characters from the note."]);
    var quote = draft.quote;
    if (quote) {
      if (!wellFormed(quote)) found.push(["quote", "Remove the unsupported character from the quote."]);
      else if (codePoints(quote) > MAX_QUOTE_CODE_POINTS) found.push(["quote", "Shorten the quote to " + count(MAX_QUOTE_CODE_POINTS) + " characters or fewer (it has " + count(codePoints(quote)) + ")."]);
      else if (TEXT_CONTROL.test(quote)) found.push(["quote", "Remove control characters from the quote."]);
    }
    var name = draft.name;
    if (name) {
      if (!wellFormed(name)) found.push(["name", "Remove the unsupported character from the name."]);
      else if (codePoints(name) > MAX_CLAIMED_AUTHOR_CODE_POINTS) found.push(["name", "Shorten the name to " + count(MAX_CLAIMED_AUTHOR_CODE_POINTS) + " characters or fewer (it has " + count(codePoints(name)) + ")."]);
      else if (LABEL_CONTROL.test(name)) found.push(["name", "Remove control or text-direction characters from the name."]);
    }
    return found;
  }
  function newNoteId() {
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    var hex = "";
    for (var i = 0; i < bytes.length; i++) hex += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
    return hex;
  }
  function slug(concept) {
    var value = String(concept).toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/-{2,}/g, "-")
      .replace(/^[-.]+|[-.]+$/g, "").slice(0, 80).replace(/[-.]+$/, "");
    return value || "page";
  }
  // The same note (and note_id) is reused only while the content is
  // byte-identical to the last one that left the composer; any edit mints a
  // new id, so two different notes never share one.
  function prepare(draft) {
    var key = keyOf(draft);
    if (draft.last && draft.last.key === key) return draft.last;
    var found = problemsOf(draft);
    if (found.length) return { problems: found };
    var note = {
      format: FORMAT, version: NOTE_VERSION, note_id: newNoteId(),
      target: { document: draft.target.document, source_sha256: draft.target.sha },
      body: draft.body
    };
    if (draft.quote) note.quote = draft.quote;
    if (draft.name) note.claimed_author = draft.name;
    if (draft.time) note.claimed_at = new Date().toISOString();
    var json = JSON.stringify(note, null, 2) + "\n";
    var size = new TextEncoder().encode(json).length;
    if (size > MAX_NOTE_BYTES) {
      return { problems: [[null, "This note is too large to import (" + count(size) + " bytes; the limit is " + count(MAX_NOTE_BYTES) + "). Shorten the note or the quote."]] };
    }
    return { key: key, note: note, json: json, filename: slug(draft.target.concept) + "-" + note.note_id.slice(0, 8) + ".okf-feedback.json" };
  }

  // ---- Styles: carried by this opt-in asset, so default output is unchanged
  var STYLE = [
    ".okf-pfn-launch{display:inline-flex;flex-wrap:wrap;align-items:center;gap:var(--okf-space-2,8px)}",
    ".okf-pfn-launch[hidden],.okf-pfn-launch__state[hidden],.okf-pfn__confirm[hidden],.okf-pfn__manual[hidden],.okf-pfn__error[hidden]{display:none}",
    ".okf-pfn-launch__state{color:var(--text-2);font-size:12.5px;font-weight:600}",
    ".okf-detail__header .okf-pfn-launch{margin-top:var(--okf-space-2,8px)}",
    ".okf-pfn{box-sizing:border-box;width:min(40rem,calc(100vw - 2rem));max-width:none;max-height:calc(100vh - 2rem);max-height:calc(100dvh - 2rem);margin:auto;padding:0;overflow:auto;overscroll-behavior:contain;border:0;border-top:2px solid var(--border-strong);border-radius:0;background:var(--surface);color:var(--text);box-shadow:var(--shadow-pop)}",
    ".okf-pfn::backdrop{background:color-mix(in srgb,var(--bg) 78%,transparent)}",
    ".okf-pfn__form{display:grid;gap:var(--okf-space-4,16px);padding:var(--okf-space-5,24px)}",
    ".okf-pfn__head{display:grid;gap:var(--okf-space-1,4px)}",
    ".okf-pfn__head h2{margin:0;font-family:var(--font-display);font-size:var(--h2-size,21px);font-weight:700;letter-spacing:-0.012em;line-height:1.2}",
    ".okf-pfn__target,.okf-pfn__hint{margin:0;color:var(--text-2);font-size:12.5px;overflow-wrap:anywhere}",
    ".okf-pfn code{font-family:var(--font-mono);font-size:.9em;overflow-wrap:anywhere}",
    ".okf-pfn__disclosure{margin:0;padding:var(--okf-space-2,8px) 0 var(--okf-space-2,8px) 12px;border-left:2px solid var(--status-info);background:transparent;color:var(--text);font-size:13px;line-height:1.55}",
    ".okf-pfn__field{display:grid;gap:var(--okf-space-1,4px)}",
    ".okf-pfn__field>label,.okf-pfn__check label{font-weight:600;font-size:13px}",
    ".okf-pfn__field textarea,.okf-pfn__field input{box-sizing:border-box;width:100%;min-height:var(--ctl-h,32px);padding:6px 10px;font:inherit;font-size:14px;border:1px solid var(--input-border);border-radius:0;background:var(--input-bg);color:var(--input-text)}",
    ".okf-pfn__field textarea{resize:vertical;min-height:4.5rem}",
    ".okf-pfn__field :is(textarea,input):focus-visible{outline:2px solid var(--focus);outline-offset:1px}",
    ".okf-pfn [aria-invalid=\"true\"]{border-color:var(--status-error);box-shadow:inset 2px 0 0 0 var(--status-error)}",
    ".okf-pfn__error{margin:0;color:var(--status-error);font-size:12.5px;font-weight:600}",
    ".okf-pfn__check{display:grid;grid-template-columns:auto 1fr;column-gap:var(--okf-space-2,8px);row-gap:var(--okf-space-1,4px);align-items:center}",
    ".okf-pfn__check input{width:1.25rem;height:1.25rem;margin:0;accent-color:var(--accent)}",
    ".okf-pfn__check .okf-pfn__hint{grid-column:2}",
    ".okf-pfn__alert,.okf-pfn__status{margin:0;font-size:13px;overflow-wrap:anywhere}",
    ".okf-pfn__alert:not(:empty){padding:var(--okf-space-2,8px) 0 var(--okf-space-2,8px) 12px;border-left:2px solid var(--status-error);background:transparent;color:var(--status-error);font-weight:600}",
    ".okf-pfn__status:not(:empty){padding:var(--okf-space-2,8px) 0 var(--okf-space-2,8px) 12px;border-left:2px solid var(--status-ok);background:transparent}",
    ".okf-pfn__confirm{display:grid;gap:var(--okf-space-2,8px);padding:var(--okf-space-2,8px) 0 var(--okf-space-2,8px) 12px;border:0;border-left:2px solid var(--status-warn);background:transparent}",
    ".okf-pfn__confirm p{margin:0;font-weight:600}",
    ".okf-pfn__row{display:flex;flex-wrap:wrap;gap:var(--okf-space-2,8px)}",
    ".okf-pfn.okf-pfn .okf-btn{min-height:44px;padding-inline:var(--okf-space-3,12px);border-radius:0}",
    ".okf-pfn .okf-pfn__manual{max-width:none;margin:0}",
    "@media (max-width:560px){.okf-pfn{width:100%;max-height:100vh;max-height:100dvh;margin:auto 0 0;border-radius:0}.okf-pfn__form{padding:var(--okf-space-4,16px)}.okf-pfn__row .okf-btn{flex:1 1 auto}}",
    "@media (forced-colors:active){.okf-pfn{border:2px solid CanvasText}.okf-pfn__disclosure,.okf-pfn__alert:not(:empty),.okf-pfn__status:not(:empty),.okf-pfn__confirm{border:1px solid CanvasText}}"
  ].join("\n");
  function injectStyles() {
    if (document.getElementById("okf-pfn-style")) return;
    var style = el("style", { id: "okf-pfn-style" });
    style.textContent = STYLE;
    (document.head || document.documentElement).appendChild(style);
  }

  // ---- Composer ------------------------------------------------------------
  function field(id, labelText, control, hintText) {
    var wrap = el("div", { "class": "okf-pfn__field" });
    control.id = id;
    var label = el("label", { "for": id }, labelText);
    var hint = el("p", { id: id + "-hint", "class": "okf-pfn__hint" }, hintText);
    var error = el("p", { id: id + "-error", "class": "okf-field-error okf-pfn__error", hidden: true });
    control.setAttribute("aria-describedby", hint.id);
    wrap.appendChild(label);
    wrap.appendChild(control);
    wrap.appendChild(hint);
    wrap.appendChild(error);
    return { wrap: wrap, control: control, hint: hint, error: error };
  }

  function buildComposer() {
    injectStyles();
    var dialog = el("dialog", { "class": "okf-pfn", "aria-labelledby": "okf-pfn-title", "aria-describedby": "okf-pfn-disclosure" });
    var form = el("form", { "class": "okf-pfn__form", novalidate: true });
    var head = el("header", { "class": "okf-pfn__head" });
    head.appendChild(el("h2", { id: "okf-pfn-title" }, "Write a feedback note"));
    var target = el("p", { "class": "okf-pfn__target", "data-okf-pfn-target": "" });
    head.appendChild(target);
    var disclosure = el("p", { id: "okf-pfn-disclosure", "class": "okf-pfn__disclosure" });

    var body = field("okf-pfn-body", "Your note (required)",
      el("textarea", { rows: 6, required: true, "aria-required": "true", autocomplete: "off" }), "");
    var quote = field("okf-pfn-quote", "Quote from this page (optional)",
      el("textarea", { rows: 3, autocomplete: "off" }),
      "Paste the passage your note is about. Up to " + count(MAX_QUOTE_CODE_POINTS) + " characters.");
    var name = field("okf-pfn-name", "Your name (optional)",
      el("input", { type: "text", autocomplete: "off", spellcheck: "false" }),
      "Anyone can type any name, so it is only an unverified claim. Up to " + count(MAX_CLAIMED_AUTHOR_CODE_POINTS) + " characters.");
    var check = el("div", { "class": "okf-pfn__check" });
    var time = el("input", { type: "checkbox", id: "okf-pfn-time", "aria-describedby": "okf-pfn-time-hint" });
    check.appendChild(time);
    check.appendChild(el("label", { "for": "okf-pfn-time" }, "Include time"));
    check.appendChild(el("p", { id: "okf-pfn-time-hint", "class": "okf-pfn__hint" },
      "Adds the current time when you download, as an unverified claim."));

    var alert = el("p", { "class": "okf-pfn__alert", role: "alert" });
    var confirm = el("div", { "class": "okf-pfn__confirm", role: "group", "aria-labelledby": "okf-pfn-confirm-text", hidden: true });
    confirm.appendChild(el("p", { id: "okf-pfn-confirm-text" }, UNSAVED));
    var confirmRow = el("div", { "class": "okf-pfn__row" });
    var keep = el("button", { type: "button", "class": "okf-btn okf-btn--primary" }, "Keep editing");
    var discard = el("button", { type: "button", "class": "okf-btn" }, "Discard note");
    confirmRow.appendChild(keep);
    confirmRow.appendChild(discard);
    confirm.appendChild(confirmRow);

    var actions = el("div", { "class": "okf-pfn__row okf-pfn__actions" });
    var download = el("button", { type: "submit", "class": "okf-btn okf-btn--primary" }, "Download note");
    var copy = el("button", { type: "button", "class": "okf-btn" }, "Copy JSON");
    var close = el("button", { type: "button", "class": "okf-btn okf-btn--quiet" }, "Close");
    actions.appendChild(download);
    actions.appendChild(copy);
    actions.appendChild(close);

    var manual = el("div", { "class": "okf-copy-fallback okf-pfn__manual", hidden: true });
    manual.appendChild(el("label", { "for": "okf-pfn-manual-text" }, "Copy the note manually"));
    manual.appendChild(el("p", { "class": "okf-copy-fallback__instruction" },
      "Copying is unavailable here. Select the text below, copy it, and save it as a .okf-feedback.json file."));
    var manualText = el("textarea", { id: "okf-pfn-manual-text", readonly: true, rows: 8, spellcheck: "false" });
    manual.appendChild(manualText);
    var selectAll = el("button", { type: "button", "class": "okf-btn" }, "Select all");
    manual.appendChild(selectAll);
    var status = el("p", { "class": "okf-pfn__status", role: "status", "aria-live": "polite" });

    [head, disclosure, body.wrap, quote.wrap, name.wrap, check, alert, confirm, actions, manual, status]
      .forEach(function (node) { form.appendChild(node); });
    dialog.appendChild(form);

    var parts = {
      dialog: dialog, form: form, target: target, disclosure: disclosure,
      fields: { body: body, quote: quote, name: name }, time: time,
      alert: alert, confirm: confirm, keep: keep, status: status,
      manual: manual, manualText: manualText, manualKey: null, launcher: null
    };
    form.addEventListener("submit", function (event) { event.preventDefault(); onDownload(); });
    form.addEventListener("input", onEdit);
    form.addEventListener("change", onEdit);
    copy.addEventListener("click", onCopy);
    close.addEventListener("click", requestClose);
    keep.addEventListener("click", keepEditing);
    discard.addEventListener("click", function () { finish("discard"); });
    selectAll.addEventListener("click", function () { manualText.focus(); manualText.select(); });
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", onClosed);
    return parts;
  }

  function renderTarget(target) {
    ui.target.textContent = "";
    ui.target.appendChild(document.createTextNode("Page: "));
    ui.target.appendChild(el("code", null, target.document));
    ui.disclosure.textContent = "";
    ui.disclosure.appendChild(document.createTextNode(
      "This note is saved only as a file you download. Nothing is sent anywhere. It targets this page's exact source (sha256 "));
    ui.disclosure.appendChild(el("code", { title: target.sha }, target.sha.slice(0, 12) + "\u2026"));
    ui.disclosure.appendChild(document.createTextNode("). Your name and time are optional and "));
    ui.disclosure.appendChild(el("strong", null, "unverified"));
    ui.disclosure.appendChild(document.createTextNode("; anyone can edit them."));
  }

  function readFields() {
    if (!current) return;
    current.body = ui.fields.body.control.value;
    current.quote = ui.fields.quote.control.value;
    current.name = ui.fields.name.control.value;
    current.time = ui.time.checked;
  }
  function updateCounter() {
    var used = codePoints(ui.fields.body.control.value);
    var over = used - MAX_BODY_CODE_POINTS;
    ui.fields.body.hint.textContent = count(used) + " of " + count(MAX_BODY_CODE_POINTS) + " characters" +
      (over > 0 ? " (" + count(over) + " over the limit)" : "");
  }
  function setStatus(text) { if (ui.status.textContent !== text) ui.status.textContent = text; }

  function showFieldError(parts, message) {
    parts.error.textContent = message;
    setHidden(parts.error, false);
    parts.control.setAttribute("aria-invalid", "true");
    parts.control.setAttribute("aria-describedby", parts.error.id + " " + parts.hint.id);
  }
  function clearFieldError(parts) {
    parts.error.textContent = "";
    setHidden(parts.error, true);
    parts.control.removeAttribute("aria-invalid");
    parts.control.setAttribute("aria-describedby", parts.hint.id);
  }
  function clearProblems() {
    Object.keys(ui.fields).forEach(function (key) { clearFieldError(ui.fields[key]); });
    ui.alert.textContent = "";
  }
  function showProblems(found) {
    clearProblems();
    var first = null;
    found.forEach(function (problem) {
      if (problem[0] && own(ui.fields, problem[0])) {
        showFieldError(ui.fields[problem[0]], problem[1]);
        if (!first) first = ui.fields[problem[0]].control;
      } else {
        ui.alert.textContent = problem[1];
      }
    });
    setStatus("");
    (first || ui.fields.body.control).focus();
  }
  // Errors already on screen follow the text; new ones wait for an action.
  function revalidateShown() {
    var found = problemsOf(current);
    Object.keys(ui.fields).forEach(function (key) {
      var parts = ui.fields[key];
      if (parts.error.hidden) return;
      var still = found.filter(function (problem) { return problem[0] === key; })[0];
      if (still) { if (parts.error.textContent !== still[1]) parts.error.textContent = still[1]; }
      else clearFieldError(parts);
    });
    if (ui.alert.textContent) ui.alert.textContent = "";
  }

  function onEdit() {
    readFields();
    updateCounter();
    revalidateShown();
    if (!ui.manual.hidden && ui.manualKey !== keyOf(current)) {
      setHidden(ui.manual, true);
      ui.manualText.value = "";
      ui.manualKey = null;
    }
    if (current.savedKey && current.savedKey !== keyOf(current) && hasContent(current)) {
      setStatus("Changed since the last download. Download again to keep your changes.");
    } else if (current.savedKey && current.savedKey === keyOf(current) && current.last) {
      setStatus("Downloaded " + current.last.filename + ". Nothing was sent.");
    }
    syncUnloadGuard();
    updateLaunchers();
  }

  function saveFile(prepared) {
    var url = URL.createObjectURL(new Blob([prepared.json], { type: "application/json" }));
    // The anchor lives inside the modal dialog, which is never inert.
    var link = el("a", { href: url, download: prepared.filename, hidden: true });
    ui.form.appendChild(link);
    try { link.click(); }
    finally {
      link.remove();
      window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    }
  }
  function onDownload() {
    readFields();
    var draft = current;
    var prepared = prepare(draft);
    if (prepared.problems) { showProblems(prepared.problems); return; }
    clearProblems();
    try { saveFile(prepared); }
    catch (e) {
      ui.alert.textContent = "The download could not start in this browser. Use Copy JSON instead.";
      return;
    }
    draft.last = prepared;
    draft.savedKey = prepared.key;
    setStatus("Downloaded " + prepared.filename + ". Nothing was sent. A local Studio operator can import this file.");
    syncUnloadGuard();
    updateLaunchers();
  }
  function showManual(prepared) {
    ui.manualText.value = prepared.json;
    ui.manualKey = prepared.key;
    setHidden(ui.manual, false);
    setStatus("Copying is unavailable here, so the note is shown below and selected. Copy it and save it as " + prepared.filename + ".");
    ui.manualText.focus();
    ui.manualText.select();
  }
  function onCopy() {
    readFields();
    var draft = current;
    var prepared = prepare(draft);
    if (prepared.problems) { showProblems(prepared.problems); return; }
    clearProblems();
    draft.last = prepared;
    var generation = ++copyGeneration;
    var clipboard = navigator.clipboard;
    if (!clipboard || typeof clipboard.writeText !== "function" || window.isSecureContext === false) {
      showManual(prepared);
      return;
    }
    clipboard.writeText(prepared.json).then(function () {
      if (generation !== copyGeneration) return;
      draft.savedKey = prepared.key;
      if (current === draft && ui.dialog.open) {
        setStatus("Copied the note. Save it as " + prepared.filename + " so a local Studio operator can import it. Nothing was sent.");
      }
      syncUnloadGuard();
      updateLaunchers();
    }, function () {
      if (generation === copyGeneration && current === draft && ui.dialog.open) showManual(prepared);
    });
  }

  function showConfirm() {
    setHidden(ui.confirm, false);
    ui.keep.focus();
  }
  function keepEditing() {
    setHidden(ui.confirm, true);
    ui.fields.body.control.focus();
  }
  // Our own closes settle synchronously; the dialog's close event is queued
  // as a later task, and the draft state must not lag behind what is shown.
  function finish(intent) {
    closeIntent = intent;
    ui.dialog.close();
    settleClosed();
  }
  function requestClose() {
    readFields();
    if (isDirty(current)) showConfirm();
    else finish("done");
  }
  function onCancel(event) {
    readFields();
    if (!ui.confirm.hidden) { event.preventDefault(); keepEditing(); return; }
    if (isDirty(current)) { event.preventDefault(); showConfirm(); return; }
    closeIntent = "done";
  }
  // A finished (empty or downloaded) or discarded draft is dropped. If the
  // browser closed the dialog on its own, the draft is kept in memory and the
  // launcher says it was not downloaded; the leave warning still guards it.
  function onClosed() {
    // Already settled by finish(), or reopened before this queued event ran.
    if (!current || ui.dialog.open) return;
    settleClosed();
  }
  function settleClosed() {
    var draft = current, intent = closeIntent;
    closeIntent = null;
    copyGeneration++;
    setHidden(ui.confirm, true);
    if (draft && (intent === "discard" || intent === "done")) delete drafts[draft.target.document];
    current = null;
    syncUnloadGuard();
    updateLaunchers();
    var launcher = ui.launcher;
    ui.launcher = null;
    if (launcher && launcher.isConnected && launcher.getClientRects().length) launcher.focus();
  }

  function openComposer(target, launcher) {
    if (!ui) ui = buildComposer();
    if (ui.dialog.open) return;
    current = draftFor(target);
    ui.launcher = launcher;
    renderTarget(target);
    ui.fields.body.control.value = current.body;
    ui.fields.quote.control.value = current.quote;
    ui.fields.name.control.value = current.name;
    ui.time.checked = !!current.time;
    clearProblems();
    setHidden(ui.confirm, true);
    setHidden(ui.manual, true);
    ui.manualText.value = "";
    ui.manualKey = null;
    ui.status.textContent = "";
    updateCounter();
    if (!ui.dialog.isConnected) document.body.appendChild(ui.dialog);
    ui.dialog.showModal();
    ui.fields.body.control.focus();
    updateLaunchers();
  }

  // ---- Launchers -----------------------------------------------------------
  var launcherCount = 0;
  function makeLauncher(resolveTarget) {
    var stateId = "okf-pfn-launch-state-" + (++launcherCount);
    var wrap = el("span", { "class": "okf-pfn-launch", "data-okf-portable-feedback": "" });
    var button = el("button", { type: "button", "class": "okf-btn okf-pfn-launch__button", "aria-haspopup": "dialog" }, "Write feedback note");
    var state = el("span", { id: stateId, "class": "okf-pfn-launch__state", hidden: true }, "Draft not downloaded");
    wrap.appendChild(button);
    wrap.appendChild(state);
    button.addEventListener("click", function () {
      var target = resolveTarget();
      if (target) openComposer(target, button);
    });
    var launcher = { wrap: wrap, button: button, state: state, resolve: resolveTarget };
    launchers.push(launcher);
    return launcher;
  }
  function updateLaunchers() {
    launchers.forEach(function (launcher) {
      var target = launcher.resolve();
      setHidden(launcher.wrap, !target);
      setAttr(launcher.wrap, "data-okf-portable-feedback-document", target ? target.document : null);
      var pending = !!(target && own(drafts, target.document) && isDirty(drafts[target.document]) &&
        !(ui && ui.dialog.open && current === drafts[target.document]));
      setHidden(launcher.state, !pending);
      setAttr(launcher.button, "aria-describedby", pending ? launcher.state.id : null);
    });
  }

  function mountPage(target) {
    var host = document.querySelector("[data-okf-page-utilities]") || document.querySelector(".okf-page__header");
    if (!host) return;
    host.appendChild(makeLauncher(function () { return target; }).wrap);
    updateLaunchers();
  }

  // Single-file: the note targets the concept on screen, resolved from the
  // same signal as Review in Studio (okf-loom:graph-selection plus the
  // reader panes). The concept list and reserved documents are not targets.
  function mountSingleFile(documents) {
    var header = document.querySelector("#detail-content .okf-detail__header");
    if (!header) return;
    var content = document.getElementById("detail-content");
    var body = document.getElementById("detail-body");
    var selected = null;
    var snapshot = window.okfGraphSelection;
    if (snapshot && snapshot.conceptId) selected = String(snapshot.conceptId);
    if (!selected && body && content && !content.hidden && body.getAttribute("data-concept-id")) selected = body.getAttribute("data-concept-id");
    function shownTarget() {
      if (document.body.getAttribute("data-okf-single-destination") === "index") return null;
      var reserved = document.getElementById("detail-document");
      if (reserved && !reserved.hidden) return null;
      if (!content || content.hidden || !selected) return null;
      return own(documents, selected) ? documents[selected] : null;
    }
    header.appendChild(makeLauncher(shownTarget).wrap);
    window.addEventListener("okf-loom:graph-selection", function (event) {
      selected = event.detail && event.detail.conceptId ? String(event.detail.conceptId) : null;
      updateLaunchers();
    });
    var pane = document.getElementById("okf-detail");
    if (pane) new MutationObserver(updateLaunchers).observe(pane, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "data-okf-document"] });
    new MutationObserver(updateLaunchers).observe(document.body, { attributes: true, attributeFilter: ["data-okf-single-destination"] });
    updateLaunchers();
  }

  function mount() {
    if (document.body.getAttribute("data-okf-mode") === "serve") return;
    if (typeof HTMLDialogElement !== "function" || typeof HTMLDialogElement.prototype.showModal !== "function") return;
    if (!window.crypto || typeof window.crypto.getRandomValues !== "function") return;
    if (typeof Blob !== "function" || !window.URL || typeof URL.createObjectURL !== "function" || typeof TextEncoder !== "function") return;
    var boot = readBootstrap();
    if (!boot) return;
    injectStyles();
    if (boot.kind === "page") mountPage(boot.target);
    else mountSingleFile(boot.documents);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
}());
