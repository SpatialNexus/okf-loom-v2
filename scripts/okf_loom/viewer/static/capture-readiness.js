/* Stable production capture-readiness boundary for live and portable viewers. */
(function () {
  "use strict";
  var generation = 0, active = null, DEADLINE_MS = 30000;
  var terminalCodes = [
    "missing-target", "hidden", "unreadable", "unavailable", "network-failure",
    "not-ready", "timeout", "superseded", "revision-mismatch", "blocked-script",
    "boot-failure", "renderer-failure", "theme-failure",
  ];
  var scriptFailures = { renderer: null, studio: null, theme: null };

  function failure(code, message, own, started, phase, cause) {
    var error = new Error(message);
    error.name = "OkfCaptureReadinessError";
    error.code = terminalCodes.indexOf(code) >= 0 ? code : "not-ready";
    error.generation = own;
    error.screenshotAllowed = false;
    error.metadata = diagnostics(own, started, phase || error.code);
    if (cause) error.metadata.cause = String(cause && cause.message || cause);
    return error;
  }
  function diagnostics(own, started, phase) {
    var vv = window.visualViewport;
    return {
      target: active && active.targetName || null,
      kind: active && active.kind || "document",
      revision: document.body && (document.body.dataset.okfCaptureRevision || document.body.dataset.okfRevision) || null,
      url: location.href,
      cssViewport: { width: innerWidth, height: innerHeight },
      dpr: Number(window.devicePixelRatio || 1),
      browserZoomEvidence: { visualViewportScale: vv ? Number(vv.scale || 1) : null, source: "visualViewport.scale; not a text-only zoom claim" },
      theme: document.documentElement.dataset.theme || (document.body && document.body.dataset.theme) || "light",
      javascript: true,
      readinessPhase: phase,
      generation: own,
      elapsedMs: Math.max(0, Date.now() - started),
      remainingMs: Math.max(0, (started + DEADLINE_MS) - Date.now())
    };
  }
  function targetFor(value) {
    if (!value || value === "document") return document.body;
    if (value === "graph") return document.getElementById("okf-graph");
    if (value === "search") return document.querySelector(".okf-search, #okf-single-search");
    if (typeof value !== "string") return null;
    try { return document.querySelector(value); } catch (error) { return null; }
  }

  function scriptURL(script) {
    if (!script) return "";
    return String((script.getAttribute && script.getAttribute("src")) || script.src || "");
  }

  function scriptKind(script) {
    var url = scriptURL(script);
    if (/renderers\.js(?:$|[?#])/.test(url)) return "renderer";
    if (/studio\.js(?:$|[?#])/.test(url)) return "studio";
    if (/theme\.js(?:$|[?#])/.test(url)) return "theme";
    return null;
  }

  function rememberScriptFailure(kind, target, message) {
    if (!kind || scriptFailures[kind]) return;
    scriptFailures[kind] = {
      kind: kind,
      url: scriptURL(target),
      message: message || (kind + " script failed to load."),
    };
    try {
      window.dispatchEvent(new CustomEvent("okf-loom:scriptFailure", {
        detail: scriptFailures[kind],
      }));
    } catch (error) { /* diagnostics remain in memory */ }
  }

  // Resource errors can fire before a capture consumer calls start(). Keep a
  // small typed record so a blocked deferred/module script is not reported as
  // a generic 30 second timeout. The listener is capture-phase because the
  // failed element's own event does not bubble.
  window.addEventListener("error", function (event) {
    var target = event && event.target;
    if (!target || String(target.tagName || "").toUpperCase() !== "SCRIPT") return;
    var kind = scriptKind(target);
    if (kind) rememberScriptFailure(kind, target, kind + " script failed to load.");
  }, true);
  Array.prototype.forEach.call(document.querySelectorAll("script"), function (script) {
    var kind = scriptKind(script);
    if (!kind) return;
    script.addEventListener("error", function (event) {
      rememberScriptFailure(kind, script, kind + " script failed to load.");
    });
  });

  function expectedScript(kind) {
    var selector = kind === "renderer" ? "script[src*='renderers.js']" :
      (kind === "studio" ? "script[src*='studio.js']" : "script[src*='theme.js']");
    return document.querySelector(selector);
  }

  function publicRendererState() {
    var state = window.okfRendererReadiness;
    if (state && typeof state === "object") return state;
    var body = document.body;
    var root = document.documentElement;
    var status = body && body.dataset.okfRenderersStatus || root.dataset.okfRenderersStatus;
    var ready = body && body.dataset.okfRenderersReady || root.dataset.okfRenderersReady;
    if (!status && ready) status = ready === "loading" ? "loading" : ready;
    if (!status) return null;
    return {
      status: status === "failed" ? "failed" : status,
      code: (body && body.dataset.okfRenderersCode) || root.dataset.okfRenderersCode || null,
      message: null,
      generation: (body && body.dataset.okfRenderersGeneration) || root.dataset.okfRenderersGeneration || null,
      bodyGeneration: (body && body.dataset.okfRenderersBodyGeneration) || root.dataset.okfRenderersBodyGeneration || null,
      themeGeneration: (body && body.dataset.okfRenderersThemeGeneration) || root.dataset.okfRenderersThemeGeneration || null,
    };
  }

  function publicStudioState() {
    var state = window.okfStudioReadiness;
    if (state && typeof state === "object") return state;
    var body = document.body;
    var root = document.documentElement;
    var status = body && body.dataset.okfStudioStatus || root.dataset.okfStudioStatus;
    if (!status && root.classList && root.classList.contains("okf-studio-booted")) status = "ready";
    if (!status) return null;
    return {
      status: status,
      code: (body && body.dataset.okfStudioCode) || root.dataset.okfStudioCode || null,
      message: null,
      generation: (body && body.dataset.okfStudioGeneration) || root.dataset.okfStudioGeneration || null,
    };
  }

  function lifecycleRequirement(owner) {
    var body = document.body;
    var root = document.documentElement;
    var rendererRequired = !!expectedScript("renderer") || !!window.okfRendererReadiness ||
      !!(body && body.dataset.okfRenderersRequired === "1");
    if (rendererRequired) {
      var renderer = publicRendererState();
      if (scriptFailures.renderer) return {
        code: "blocked-script", phase: "renderers-script",
        message: "The document renderer script was blocked.",
      };
      if (!renderer) {
        if (document.readyState === "complete") return {
          code: "blocked-script", phase: "renderers-script",
          message: "The document renderer script did not publish a terminal state.",
        };
        return { pending: true, phase: "deferred-renderers" };
      }
      if (renderer.status === "loading" || renderer.status === "pending") {
        return { pending: true, phase: "deferred-renderers" };
      }
      if (renderer.status === "failed" || renderer.status === "error") return {
        code: renderer.code || "renderer-failure",
        phase: "renderers-" + (renderer.code || "failure"),
        message: renderer.message || "The document renderer could not settle.",
      };
      if (renderer.status !== "ready") return { pending: true, phase: "deferred-renderers" };
    }

    // A served page with a bootstrap block is expected to expose the usable
    // Studio API. A static page has neither block nor studio script and does
    // not wait for an editor that it intentionally does not load.
    var studioRequired = !!document.getElementById("okf-studio-bootstrap") ||
      !!expectedScript("studio") || !!window.okfStudioReadiness;
    if (studioRequired) {
      var studio = publicStudioState();
      if (scriptFailures.studio) return {
        code: "blocked-script", phase: "studio-script",
        message: "The Studio script was blocked.",
      };
      if (!studio) {
        if (document.readyState === "complete") return {
          code: "blocked-script", phase: "studio-script",
          message: "The Studio script did not publish a terminal state.",
        };
        return { pending: true, phase: "studio-boot" };
      }
      if (studio.status === "loading" || studio.status === "pending") {
        return { pending: true, phase: "studio-boot" };
      }
      if (studio.status === "failed" || studio.status === "error") return {
        code: studio.code || "boot-failure",
        phase: "studio-" + (studio.code || "failure"),
        message: studio.message || "Studio failed during boot.",
      };
      if (studio.status === "ready" && (!window.okfLoomStudio ||
          typeof window.okfLoomStudio !== "object" ||
          typeof window.okfLoomStudio.openPanel !== "function" ||
          typeof window.okfLoomStudio.setView !== "function")) return {
        code: "boot-failure", phase: "studio-api", message: "Studio reported ready without a usable API.",
      };
      if (studio.status !== "ready") return { pending: true, phase: "studio-boot" };
    }

    // theme.js is loaded before capture-readiness on every shipped viewer
    // surface. data-theme alone is only server paint; captures require the
    // runtime owner to have completed and to agree with the resolved root.
    var themeExpected = root.hasAttribute("data-theme") || !!expectedScript("theme") ||
      !!(body && body.classList && body.classList.contains("okf-viewer"));
    if (themeExpected) {
      if (scriptFailures.theme) return {
        code: "blocked-script", phase: "theme-script",
        message: "The theme runtime script was blocked.",
      };
      if (!root.classList || !root.classList.contains("okf-theme-ready") ||
          !window.OKFLoomTheme || typeof window.OKFLoomTheme.getResolvedTheme !== "function") {
        return { pending: true, phase: "theme-runtime" };
      }
      var resolved;
      try { resolved = window.OKFLoomTheme.getResolvedTheme(); } catch (error) {
        return { code: "theme-failure", phase: "theme-runtime", cause: error, message: "Theme runtime did not resolve." };
      }
      if (typeof resolved !== "string" || resolved !== root.dataset.theme) {
        return { pending: true, phase: "theme-resolution" };
      }
    }
    return null;
  }
  function visible(target) {
    var style = getComputedStyle(target), rect = target.getBoundingClientRect();
    return !target.hidden && style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  }
  function canvasReadable(target, kind) {
    // Document readiness belongs to the visible document surface. Portable
    // single-file Index keeps an inactive graph canvas in the same body; that
    // hidden descendant must not relabel the visible Index as an invalid canvas
    // capture. Only graph ownership (or an explicitly targeted canvas) opts in.
    var isCanvas = target.matches && target.matches("canvas");
    if (!isCanvas && kind !== "graph") return { readable: true, canvas: false };
    var canvas = isCanvas ? target : target.querySelector("canvas");
    if (!canvas) return { readable: true, canvas: false };
    if (!visible(canvas) || canvas.width <= 0 || canvas.height <= 0) return { readable: false, code: "hidden" };
    try { canvas.toDataURL("image/png"); return { readable: true, canvas: true }; }
    catch (error) { return { readable: false, code: "unreadable" }; }
  }
  function inspect(owner) {
      var target = targetFor(owner.targetName);
      if (!target) return { code: "missing-target", message: "Capture target does not exist." };
      var state = owner.kind === "search" ? (document.body.dataset.okfSearchCaptureReady || document.body.dataset.okfCaptureReady) : (owner.kind === "graph" ? (document.body.dataset.okfGraphCaptureReady || document.body.dataset.okfCaptureReady) : document.body.dataset.okfCaptureReady);
        if (owner.kind !== "graph" && state === "unavailable") return { code: "unavailable", phase: owner.kind + "-unavailable", message: "Capture surface is unavailable." };
        if (owner.kind !== "graph" && state === "network-failure") return { code: "network-failure", message: "Capture surface failed to load network data." };
      var lifecycle = lifecycleRequirement(owner);
      if (lifecycle) return lifecycle;
      if (!visible(target)) return { code: "hidden", message: "Capture target is hidden or has zero bounds." };
    var readable = canvasReadable(target, owner.kind);
    if (!readable.readable) return { code: readable.code, message: readable.code === "unreadable" ? "Capture canvas cannot be read." : "Capture canvas is hidden or has zero bounds." };
    var revision = document.body.dataset.okfCaptureRevision || document.body.dataset.okfRevision || null;
    if (owner.expectedRevision && revision !== owner.expectedRevision) return { code: "revision-mismatch", message: "Capture revision does not match the expected revision." };
    if (owner.kind === "graph") {
      var graph;
        try {
          graph = window.okfGraphCaptureState && window.okfGraphCaptureState();
      } catch (error) {
        return { code: "unavailable", phase: "graph-adapter", cause: error, message: "Graph capture state is unavailable." };
        }
        function invalid(message) { return { code: "unavailable", phase: "graph-adapter-invalid", message: message }; }
        if (graph == null) return { pending: true, phase: "graph-initializing" };
        if (typeof graph !== "object" || Array.isArray(graph)) return { code: "unavailable", phase: "graph-adapter-invalid", message: "Graph capture adapter is malformed." };
        if (typeof graph.initialized !== "boolean" || typeof graph.layoutRequired !== "boolean" || typeof graph.layoutSettled !== "boolean") return invalid("Graph capture adapter booleans are malformed.");
        if (graph.suspended != null && typeof graph.suspended !== "boolean") return invalid("Graph suspension readiness is malformed.");
        if (graph.transitionPending != null && typeof graph.transitionPending !== "boolean") return invalid("Graph transition readiness is malformed.");
        var numeric = [graph.nodeCount, graph.payloadNodeCount, graph.layoutGeneration, graph.appliedLayoutGeneration];
        if (numeric.some(function (value) { return typeof value !== "number" || !Number.isFinite(value) || value < 0 || Math.floor(value) !== value; })) return invalid("Graph capture adapter counters are malformed.");
        if (graph.nodeCount > graph.payloadNodeCount) return invalid("Rendered graph inventory exceeds the authoritative payload.");
          var hasTerminal = graph.terminalCode != null || graph.terminalPhase != null;
          if (hasTerminal && (typeof graph.terminalCode !== "string" || !graph.terminalCode || typeof graph.terminalPhase !== "string" || !graph.terminalPhase)) return invalid("Graph capture terminal state is malformed.");
          if (hasTerminal) return { code: graph.terminalCode, phase: graph.terminalPhase, message: "Graph capture surface is unavailable." };
          if (graph.suspended) return { code: "superseded", phase: "graph-suspended", message: "Graph capture is suspended while the page is in BFCache." };
          if (graph.transitionPending) return { pending: true, phase: "graph-transition" };
          if (graph.initialized && (typeof revision !== "string" || !revision.trim())) return invalid("Graph capture revision is missing.");
        if (!graph.initialized && revision) return invalid("An uninitialized graph cannot own a capture revision.");
        if (!graph.initialized && (graph.nodeCount || graph.payloadNodeCount || graph.layoutRequired || graph.layoutSettled || graph.layoutGeneration || graph.appliedLayoutGeneration)) return invalid("An uninitialized graph exposes committed rendering state.");
        if (!graph.initialized && !hasTerminal) return { pending: true, phase: "graph-initializing" };
          if (graph.initialized && !graph.layoutSettled) {
            if (graph.layoutRequired && graph.layoutGeneration > 0 && graph.appliedLayoutGeneration < graph.layoutGeneration) return { pending: true, phase: "graph-layout" };
            return invalid("An unsettled graph does not own coherent layout work.");
        }
        if ((graph.layoutGeneration || graph.appliedLayoutGeneration) && graph.layoutGeneration !== graph.appliedLayoutGeneration) return invalid("Graph layout generations contradict each other.");
        if (!graph.layoutRequired && (graph.layoutGeneration !== 0 || graph.appliedLayoutGeneration !== 0)) return invalid("A graph without layout work must have zero generations.");
        if (graph.layoutRequired && (!graph.layoutGeneration || !graph.appliedLayoutGeneration)) return invalid("A required graph layout must identify its settled generation.");
        if (graph.payloadNodeCount > 0 && graph.nodeCount < 1) return { pending: true, phase: "graph-inventory" };
          if (state === "unavailable") return { code: "unavailable", phase: "graph-unavailable", message: "Capture surface is unavailable." };
          if (state === "network-failure") return { code: "network-failure", phase: "graph-network-failure", message: "Capture surface failed to load network data." };
    }
    if (state && state !== "ready") return { pending: true, phase: state };
    return { ready: true, phase: owner.kind === "graph" ? "graph-ready" : "ready" };
  }
    function finish(owner, error, phase) {
      if (!active || active.generation !== owner.generation) return false;
      var successMetadata = error ? null : diagnostics(owner.generation, owner.started, phase);
      if (owner.signal && owner.abortHandler) owner.signal.removeEventListener("abort", owner.abortHandler);
      clearTimeout(owner.timer); cancelAnimationFrame(owner.frame); active = null;
    if (error) owner.reject(error);
    else owner.resolve({ generation: owner.generation, screenshotAllowed: true, metadata: successMetadata });
    return true;
  }
  function poll(owner) {
    if (!active || active.generation !== owner.generation) return;
    var result = inspect(owner);
    if (result.ready) { finish(owner, null, result.phase); return; }
    if (result.code && result.code !== "not-ready") { finish(owner, failure(result.code, result.message, owner.generation, owner.started, result.phase || result.code, result.cause)); return; }
    owner.phase = result.phase || "not-ready";
    owner.frame = requestAnimationFrame(function () { poll(owner); });
  }
  function start(options) {
    options = options || {};
    var own = ++generation, started = Date.now(), targetName = options.target || "document";
    if (active) finish(active, failure("superseded", "Capture readiness was superseded by a newer generation.", active.generation, active.started, "superseded"));
    var resolvePromise, rejectPromise;
    var promise = new Promise(function (resolve, reject) { resolvePromise = resolve; rejectPromise = reject; });
      var owner = { generation: own, started: started, targetName: targetName, kind: targetName === "graph" ? "graph" : (targetName === "search" ? "search" : "document"), expectedRevision: options.expectedRevision || null, resolve: resolvePromise, reject: rejectPromise, frame: 0, timer: 0, phase: "starting", signal: options.signal || null, abortHandler: null };
    active = owner;
    owner.timer = setTimeout(function () { finish(owner, failure("timeout", "Capture readiness exceeded the single 30000ms deadline.", own, started, owner.phase)); }, DEADLINE_MS);
      if (options.signal) {
        if (options.signal.aborted) finish(owner, failure("superseded", "Capture readiness was aborted.", own, started, "aborted"));
        else { owner.abortHandler = function () { finish(owner, failure("superseded", "Capture readiness was aborted.", own, started, "aborted")); }; options.signal.addEventListener("abort", owner.abortHandler, { once: true }); }
    }
    queueMicrotask(function () { poll(owner); });
    return { generation: own, deadlineAt: started + DEADLINE_MS, promise: promise };
  }
  window.addEventListener("pagehide", function () { if (active) finish(active, failure("superseded", "Capture readiness ended because the page was hidden.", active.generation, active.started, "pagehide")); });
  window.okfCaptureReadiness = Object.freeze({ start: start, deadlineMs: DEADLINE_MS, terminalCodes: Object.freeze(terminalCodes.slice()) });
}());
