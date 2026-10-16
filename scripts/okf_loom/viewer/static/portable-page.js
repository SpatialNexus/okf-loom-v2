/* Static graph/search transport admission. Documents and Index need no fetch. */
(function () {
  "use strict";
  var owner = document.currentScript;
  if (!owner) return;
  var runtime = owner.getAttribute("data-okf-runtime");
  if (!runtime) return;
  if (location.protocol !== "file:") {
    var script = document.createElement("script");
    script.src = new URL(runtime, owner.src).href;
    script.async = false;
    document.body.appendChild(script);
    return;
  }

  /* A file-origin fetch cannot read the emitted JSON. Do not start a doomed
   * request or offer Retry/graph controls that cannot change that constraint. */
  var body = document.body, main = document.getElementById("okf-main");
  if (!main) return;
  body.setAttribute("data-okf-js", "1");
  body.dataset.okfCaptureReady = "unavailable";
  delete body.dataset.okfCaptureRevision;
  if (runtime === "graph.js") {
    body.dataset.okfGraphCaptureReady = "unavailable";
    window.okfGraphCaptureState = function () {
      return { initialized: false, nodeCount: 0, payloadNodeCount: 0,
        layoutRequired: false, layoutSettled: false, layoutGeneration: 0,
        appliedLayoutGeneration: 0, terminalCode: "unavailable",
        terminalPhase: "graph-protocol", terminalMessage: "This site needs a web server." };
    };
  }
  var heading = main.querySelector(".okf-graph-route-title") || main.querySelector("h1");
  main.replaceChildren();
  if (heading) main.appendChild(heading);
  body.classList.remove("okf-viewer--graph");
  main.className = "okf-search";
  var banner = document.querySelector(".okf-static-viewer-note");
  if (banner) banner.remove();
  var note = document.createElement("section");
  note.className = "okf-portable-server-note";
  note.setAttribute("role", "status");
  var title = document.createElement("h2");
  title.textContent = "This site needs a web server";
  var explanation = document.createElement("p");
  explanation.textContent = "Graph and Search cannot load their data when this static site is opened from file://. Serve this build folder over HTTP, for example with ";
  var command = document.createElement("code");
  command.textContent = "python -m http.server";
  explanation.appendChild(command);
  explanation.appendChild(document.createTextNode(
    ", then open http://localhost:8000. Index and documents also work directly from disk."));
  var index = document.createElement("a");
  index.href = "index.html";
  index.className = "okf-btn";
  index.textContent = "Browse the concept index";
  note.appendChild(title);
  note.appendChild(explanation);
  note.appendChild(index);
  main.appendChild(note);
}());
