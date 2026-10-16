/* OKF viewer - graph view + detail panel logic.
 *
 * Used in TWO contexts:
 *   1. The single-file viewer (template inlines this + bundle data).
 *   2. The full-page /__graph view (fetches graph.json from the server).
 *
 * Mode detection:
 *   - If window.BUNDLE is set → single-file mode (data already inlined).
 *   - Else if window.OKF_LOOM_GRAPH_DATA_URL is set → fetch graph JSON.
 *
 * Optional window flags:
 *   - OKF_LOOM_INITIAL_LAYOUT (default "cose")
 *   - OKF_CONCEPT_PAGE_PREFIX (default "/") - base URL for opening a concept
 *     page when "Open page" is clicked (graph view only).
 *
 * CDN deps: Cytoscape.js 3.x only. Markdown bodies are pre-rendered
 * server-side by okf-loom's escaping renderer, so no client-side
 * markdown parser (marked/DOMPurify) is needed. If Cytoscape fails to
 * load, the graph area shows a helpful error instead of throwing.
 */
(function () {
  "use strict";
  document.body.setAttribute("data-okf-js", "1");

  // Theme state (the Ledger palette choice, System/OS resolution,
  // persistence, the Appearance popover) is owned ENTIRELY by
  // viewer/static/theme.js. This file is a READ-ONLY consumer: it resolves
  // the canvas colours from the active palette's tokens and re-syncs the
  // canvas on `okf-loom:themeChanged`. It never reads or writes localStorage
  // for theme and never sets data-theme.

  // ---- Canvas colours: resolved from the Ledger tokens --------------------
  // Cytoscape paints on <canvas> and cannot read CSS custom properties, so
  // the graph reads the active palette's tokens (the wiki.css palette
  // blocks) with getComputedStyle. The resolved set is cached per data-theme
  // and dropped on every palette change, so nodes, labels, edges, halos,
  // badges, glyphs, the minimap and the lens ramps all repaint from the
  // palette actually on screen. There is no per-theme colour table here: a
  // palette is defined entirely by its tokens.
  //   node        --graph-node        neutral round node fill
  //   nodeIcon    --graph-node-icon   type glyph stroke
  //   nodeBorder  --border-strong     1.5px node stroke
  //   nodeText    --graph-label       node label ink
  //   halo        --graph-halo        label halo and relationship-label plate
  //   edge        --graph-edge        edge line and arrowhead
  //   edgeLabel   --text-3            muted relationship label ink (AA on the halo)
  //   select      --accent            the selected node's 4px stroke / edge
  //   cue         --text              path, search and bridge marks (ink)
  //   raised      --surface-2         neutral hub / hover / focus halos
  //   surface     --surface           the selected/hovered label's plate
  //   chip        --bg-2              count-chip fill
  //   warn/error/info --status-*      status markers: strokes and glyphs only
  var GRAPH_TOKENS = {
    canvas: "--graph-bg", node: "--graph-node", nodeIcon: "--graph-node-icon",
    nodeBorder: "--border-strong", nodeText: "--graph-label", halo: "--graph-halo",
    edge: "--graph-edge", edgeLabel: "--text-3", select: "--accent", cue: "--text",
    muted: "--text-3", raised: "--surface-2", surface: "--surface", chip: "--bg-2",
    warn: "--status-warn", error: "--status-error", info: "--status-info"
  };
  // Last-resort values (Bone) for an embedder that loads graph.js without the
  // Ledger stylesheet; every viewer surface resolves the tokens above.
  var GRAPH_TOKEN_FALLBACK = {
    canvas: "#f6f3ed", node: "#d2cabd", nodeIcon: "#1d1a16", nodeBorder: "#b0a696",
    nodeText: "#1d1a16", halo: "#f6f3ed", edge: "#a39a8b", edgeLabel: "#675f55",
    select: "#a3573a", cue: "#1d1a16", muted: "#6a6258", raised: "#e3ddd2", surface: "#fbf9f5",
    chip: "#eae5dc", warn: "#7f5410", error: "#9e2f2a", info: "#36597e"
  };

  // Colour arithmetic for the palette-derived lens ramp. Parses the token
  // values (hex/rgb; anything else is resolved by the canvas engine) and
  // works in OKLab so equal steps read as equal steps.
  var _colourCtx = null;
  function parseColour(value) {
    var s = String(value || "").trim();
    if (!s) return null;
    var m = s.match(/^#([0-9a-f]{3,8})$/i);
    if (m) {
      var h = m[1];
      if (h.length === 3 || h.length === 4) h = h.split("").map(function (ch) { return ch + ch; }).join("");
      if (h.length !== 6 && h.length !== 8) return null;
      return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16),
        h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1];
    }
    m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+)(%?))?\s*\)$/i);
    if (m) return [+m[1], +m[2], +m[3], m[4] == null ? 1 : (m[5] ? m[4] / 100 : +m[4])];
    try {
      if (!_colourCtx) { var cv = document.createElement("canvas"); cv.width = cv.height = 1; _colourCtx = cv.getContext("2d"); }
      if (!_colourCtx) return null;
      _colourCtx.fillStyle = "#010203"; _colourCtx.fillStyle = s;
      var out = String(_colourCtx.fillStyle);
      if (out === "#010203" || out === s) return null;
      return parseColour(out);
    } catch (e) { return null; }
  }
  function colourHex(rgb) {
    return "#" + rgb.slice(0, 3).map(function (v) {
      var c = Math.max(0, Math.min(255, Math.round(v))).toString(16);
      return c.length === 1 ? "0" + c : c;
    }).join("");
  }
  function _srgbToLinear(v) { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
  function _linearToSrgb(v) { return 255 * (v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055); }
  function luminance(rgb) {
    return 0.2126 * _srgbToLinear(rgb[0]) + 0.7152 * _srgbToLinear(rgb[1]) + 0.0722 * _srgbToLinear(rgb[2]);
  }
  function contrastRatio(a, b) {
    var la = luminance(a), lb = luminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  function toOklab(rgb) {
    var r = _srgbToLinear(rgb[0]), g = _srgbToLinear(rgb[1]), b = _srgbToLinear(rgb[2]);
    var l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    var m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    var s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    return [0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
      1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s];
  }
  // OKLab -> sRGB (0..255), or null when the colour is outside the gamut.
  function fromOklab(lab) {
    var l = Math.pow(lab[0] + 0.3963377774 * lab[1] + 0.2158037573 * lab[2], 3);
    var m = Math.pow(lab[0] - 0.1055613458 * lab[1] - 0.0638541728 * lab[2], 3);
    var s = Math.pow(lab[0] - 0.0894841775 * lab[1] - 1.2914855480 * lab[2], 3);
    var lin = [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
      -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
      -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s];
    if (lin.some(function (v) { return v < -0.0005 || v > 1.0005; })) return null;
    return lin.map(function (v) { return _linearToSrgb(Math.max(0, Math.min(1, v))); });
  }
  // Lightness/chroma/hue -> sRGB, shedding chroma until the colour fits.
  function oklchRgb(L, C, h) {
    L = Math.max(0, Math.min(1, L));
    for (var c = C; c >= 0; c -= 0.004) {
      var rgb = fromOklab([L, c * Math.cos(h), c * Math.sin(h)]);
      if (rgb) return rgb;
    }
    return fromOklab([L, 0, 0]) || [L * 255, L * 255, L * 255];
  }

  // Mix `from` toward `toward` (in sRGB steps of 5%) until it reaches
  // `ratio` against `bg`; returns `from` unchanged when it already does.
  function liftToContrast(from, toward, bg, ratio) {
    for (var t = 0; t <= 1.0001; t += 0.05) {
      var mixed = [0, 1, 2].map(function (i) { return Math.round(from[i] + (toward[i] - from[i]) * t); });
      if (contrastRatio(mixed, bg) >= ratio) return mixed;
    }
    return toward.slice(0, 3);
  }

  // The lens ramp: a MUTED colour set derived from the active palette, so a
  // lens keeps its meaning without breaking the one-accent rule.
  //   categorical  8 low-chroma hues stepped by the golden angle from the
  //                accent hue, near the neutral node's lightness
  //                (themes/communities, groups). Dark palettes lift the
  //                node lightness; a light palette (Bone) steps just below
  //                its node at a lower chroma, because the same chroma at
  //                paper lightness reads as pastel candy rather than as
  //                the muted tints the dark palettes get.
  //   sequence(t)  neutral node (t=0) -> a muted accent (t=1), in OKLab
  //                (freshness, bridging, importance).
  //   relation     the categorical hues at the edge's lightness (typed
  //                edges under the Relations/Flow intent).
  // Every fill keeps the type glyph (--graph-node-icon) at >= 4.5:1.
  var GOLDEN_ANGLE = 2.399963;
  function buildLensRamp(pal) {
    var icon = pal.rgb.nodeIcon, node = toOklab(pal.rgb.node), acc = toOklab(pal.rgb.select);
    var edge = toOklab(pal.rgb.edge), dark = pal.dark;
    var accH = Math.atan2(acc[2], acc[1]), accC = Math.hypot(acc[1], acc[2]);
    function legible(L, C, h) {
      var rgb = oklchRgb(L, C, h);
      for (var i = 0; i < 20 && contrastRatio(rgb, icon) < 4.5; i++) {
        L += dark ? -0.02 : 0.02;
        rgb = oklchRgb(L, C, h);
      }
      return rgb;
    }
    var catL = node[0] + (dark ? 0.12 : -0.07), catC = dark ? 0.05 : 0.034;
    var categorical = [], relation = [];
    for (var k = 0; k < 8; k++) {
      categorical.push(colourHex(legible(catL, catC, accH + k * GOLDEN_ANGLE)));
      relation.push(colourHex(liftToContrast(oklchRgb(edge[0], 0.07, accH + k * GOLDEN_ANGLE), pal.rgb.muted, pal.rgb.canvas, 3.05)));
    }
    var hiL = node[0] + (dark ? 0.16 : -0.11), hiC = Math.min(accC * 0.75, 0.09);
    var lo = node, hi = [hiL, hiC * Math.cos(accH), hiC * Math.sin(accH)];
    var cache = {};
    function sequence(t) {
      t = Math.max(0, Math.min(1, Number(t) || 0));
      var key = Math.round(t * 100);
      if (cache[key]) return cache[key];
      var u = key / 100, lab = [lo[0] + (hi[0] - lo[0]) * u, lo[1] + (hi[1] - lo[1]) * u, lo[2] + (hi[2] - lo[2]) * u];
      return (cache[key] = colourHex(legible(lab[0], Math.hypot(lab[1], lab[2]), Math.atan2(lab[2], lab[1]))));
    }
    return { categorical: categorical, relation: relation, sequence: sequence, empty: pal.canvas };
  }

  var _graphPaletteCache = null, _graphPaletteKey = null;
  function invalidateGraphPalette() { _graphPaletteCache = null; _graphPaletteKey = null; }
  // Resolve the palette for the CURRENT data-theme (cached until the next
  // palette change). Callers must treat the result as read-only.
  function graphPalette() {
    var key = document.documentElement.getAttribute("data-theme") || "bone";
    if (_graphPaletteCache && _graphPaletteKey === key) return _graphPaletteCache;
    var cs = null;
    try { cs = window.getComputedStyle(document.documentElement); } catch (e) {}
    var pal = { theme: key, rgb: {} };
    Object.keys(GRAPH_TOKENS).forEach(function (name) {
      var rgb = parseColour(cs ? cs.getPropertyValue(GRAPH_TOKENS[name]) : "") || parseColour(GRAPH_TOKEN_FALLBACK[name]);
      pal.rgb[name] = rgb;
      pal[name] = colourHex(rgb);
    });
    pal.dark = luminance(pal.rgb.canvas) < 0.2;
    // Edges are meaningful graphics (WCAG 1.4.11): keep --graph-edge's hue
    // but lift it toward --text-3 until it clears 3:1 on the canvas. The
    // Ledger edge tokens sit near 2.5:1, so every palette takes a small,
    // palette-derived lift rather than a hard-coded colour.
    pal.rgb.edge = liftToContrast(pal.rgb.edge, pal.rgb.muted, pal.rgb.canvas, 3.05);
    pal.edge = colourHex(pal.rgb.edge);
    pal.font = (cs && cs.getPropertyValue("--font-body").trim()) || "\"Public Sans\", sans-serif";
    pal.mono = (cs && cs.getPropertyValue("--font-mono").trim()) || "\"Azeret Mono\", monospace";
    // Stateful-cue names shared with the synchronisers below.
    pal.edgeLabelBg = pal.halo;
    pal.bridgeBorder = pal.cue;
    pal.ramp = buildLensRamp(pal);
    _graphPaletteCache = pal; _graphPaletteKey = key;
    return pal;
  }

  // Forced colours (Windows High Contrast). The browser never adjusts canvas
  // pixels, so the graph resolves the active system colours itself from a
  // probe (forced-color-adjust:none keeps the keyword's own value instead of
  // the forced `color`) and paints the canvas from them. Chrome reports
  // Highlight with 0.8 alpha; the canvas paints the opaque system colour so
  // strokes never blend into a non-system tint. Cached per sync pass;
  // syncGraphTheme invalidates it on every palette/media change.
  var SYSTEM_COLOUR_KEYWORDS = {
    canvas: "Canvas", text: "CanvasText", highlight: "Highlight", link: "LinkText", gray: "GrayText", buttonText: "ButtonText", buttonFace: "ButtonFace"
  };
  var _systemColourCache = null;
  function invalidateSystemColours() { _systemColourCache = null; }
  function systemColours() {
    if (_systemColourCache) return _systemColourCache;
    var out = {}, host = document.body || document.documentElement, probe = document.createElement("span");
    probe.style.position = "fixed";
    probe.style.visibility = "hidden";
    probe.style.forcedColorAdjust = "none";
    host.appendChild(probe);
    Object.keys(SYSTEM_COLOUR_KEYWORDS).forEach(function (name) {
      probe.style.color = SYSTEM_COLOUR_KEYWORDS[name];
      var value = window.getComputedStyle(probe).color;
      var rgb = value.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/);
      out[name] = rgb ? "rgb(" + Math.round(+rgb[1]) + ", " + Math.round(+rgb[2]) + ", " + Math.round(+rgb[3]) + ")" : value;
    });
    probe.remove();
    return (_systemColourCache = out);
  }
  // The ink baked into SVG glyph and badge data URIs: the Ledger palette, or
  // the system pair while forced colours own the canvas. Normal mode returns
  // graphPalette() itself, so its URIs are byte-identical to the palette's.
  function graphInk() {
    if (!FORCED_COLORS) return graphPalette();
    var sys = systemColours();
    return { nodeIcon: sys.text, nodeText: sys.text, nodeBorder: sys.text, chip: sys.canvas, halo: sys.canvas, error: sys.text };
  }

  // ---- Config from data-* attributes (CSP-safe; no inline script) ------
  // The graph_page.html template passes config via data-* attributes on
  // #okf-graph instead of window.* globals, so the server's CSP
  // (script-src 'self' - no 'unsafe-inline') doesn't block initialisation.
  var graphEl = document.getElementById("okf-graph");
  // Graph pages do not load wiki.js. Consume the same shell-navigation focus
  // provenance here, before graph controls/tours initialize, and retain the
  // signal so an automatic coach mark cannot steal the explicit route target.
  var routeFocusRequested=false;
  var graphFocusKey="okf-shell-route-focus-v1";
  var graphFocusWindowPrefix=graphFocusKey+":";
  var graphRouteHeading=document.querySelector(".okf-graph-route-title");
  function requestGraphRouteFocus(){
    routeFocusRequested=true;
    if(!graphRouteHeading)return;
    graphRouteHeading.setAttribute("data-okf-route-heading","");graphRouteHeading.tabIndex=-1;
    var focusGraphRoute=function(){if(graphRouteHeading.isConnected)graphRouteHeading.focus({preventScroll:true});};
    window.requestAnimationFrame(focusGraphRoute);window.addEventListener("load",focusGraphRoute,{once:true});
  }
  try {
    var expectedGraphRoute=sessionStorage.getItem(graphFocusKey);
    var currentGraphRoute=window.location.pathname+window.location.search;
    if(expectedGraphRoute===currentGraphRoute){
      sessionStorage.removeItem(graphFocusKey);requestGraphRouteFocus();
    }else if(expectedGraphRoute)sessionStorage.removeItem(graphFocusKey);
  }catch(routeFocusError){
    try{
      var expectedWindowGraphRoute=window.name&&window.name.indexOf(graphFocusWindowPrefix)===0
        ?window.name.slice(graphFocusWindowPrefix.length):null;
      if(expectedWindowGraphRoute!==null)window.name="";
      if(expectedWindowGraphRoute===window.location.pathname+window.location.search)requestGraphRouteFocus();
    }catch(windowRouteFocusError){}
  }
  // The portable template predates the graph-page body modifier.  Promote it
  // at runtime so graph-only mobile chrome never inherits the compact,
  // ellipsised document header used by ordinary concept pages.
  if (graphEl) document.body.classList.add("okf-viewer--graph");
  var liveStatus=document.querySelector('[aria-label^="Live updates connection"]');if(liveStatus&&!liveStatus.hasAttribute("tabindex"))liveStatus.tabIndex=0;
  var paneNav=document.querySelector(".okf-graph-mobile-nav"),paneNavHome=paneNav&&document.createComment("okf graph pane nav home");
  if(paneNav&&paneNav.parentNode)paneNav.parentNode.insertBefore(paneNavHome,paneNav);
  var mobileSecondary=null,mobileSecondaryRow=null,mobileSecondaryPanel=null,mobileSecondaryToggle=null;
  var graphTools=document.querySelector(".okf-graph-tools");
  var graphToolsHome=graphTools&&document.createComment("okf graph tools home");
  if(graphTools&&graphTools.parentNode)graphTools.parentNode.insertBefore(graphToolsHome,graphTools);
    (function buildMobileSecondaryChrome(){var topbar=document.querySelector(".okf-topbar");if(!topbar||!graphTools)return;mobileSecondary=document.createElement("section");mobileSecondary.className="okf-mobile-secondary";mobileSecondary.setAttribute("aria-label","Graph tools and status");mobileSecondaryRow=document.createElement("div");mobileSecondaryRow.className="okf-mobile-secondary__summary";mobileSecondaryToggle=document.createElement("button");mobileSecondaryToggle.type="button";mobileSecondaryToggle.className="okf-mobile-secondary__toggle";mobileSecondaryToggle.setAttribute("aria-expanded","false");mobileSecondaryToggle.setAttribute("aria-controls","okf-mobile-secondary-controls");mobileSecondaryToggle.textContent="More tools";mobileSecondaryPanel=document.createElement("div");mobileSecondaryPanel.id="okf-mobile-secondary-controls";mobileSecondaryPanel.className="okf-mobile-secondary__controls";mobileSecondaryPanel.hidden=true;mobileSecondary.appendChild(mobileSecondaryRow);mobileSecondary.appendChild(mobileSecondaryPanel);topbar.appendChild(mobileSecondary);mobileSecondaryToggle.addEventListener("click",function(){var open=mobileSecondaryPanel.hidden;mobileSecondaryPanel.hidden=!open;mobileSecondaryToggle.setAttribute("aria-expanded",open?"true":"false");mobileSecondaryToggle.textContent=open?"Fewer tools":"More tools";});})();
  function syncCriticalMobileChrome(narrow){if(!mobileSecondaryRow)return;var focused=document.activeElement,moved=false,topbar=document.querySelector(".okf-topbar");function place(parent,node,before){if(!parent||!node)return;if(node.parentNode===parent&&node.nextSibling===before)return;parent.insertBefore(node,before);moved=true;}function placeAfter(anchor,node){if(!anchor||!anchor.parentNode||!node)return;if(node.parentNode===anchor.parentNode&&node.previousSibling===anchor)return;anchor.parentNode.insertBefore(node,anchor.nextSibling);moved=true;}if(narrow){/* Keep three explicit chrome layers in source and visual order: brand,
     pane tabs, then secondary utilities. Search/filter/reset remain available
     behind More graph tools instead of consuming the graph's initial viewport. */place(topbar,paneNav,mobileSecondary);placeAfter(graphToolsHome,graphTools);mobileSecondary.hidden=true;place(mobileSecondaryRow,mobileSecondaryToggle,null);place(topbar.querySelector('.okf-topbar__brand'),mobileModeIndicator,null);}else{placeAfter(paneNavHome,paneNav);placeAfter(graphToolsHome,graphTools);if(mobileModeIndicator&&mobileModeHome)placeAfter(mobileModeHome,mobileModeIndicator);}/* Reparenting an active control can make browsers drop focus. Restore the
     same connected control without scrolling; idempotent syncs do no focus work. */if(moved&&focused&&focused!==document.body&&focused.isConnected&&document.activeElement!==focused&&typeof focused.focus==="function"){try{focused.focus({preventScroll:true});}catch(e){focused.focus();}}}
    /* The narrow chrome follows the visual viewport (pinch zoom keeps it on
     screen) but never exceeds the initial containing block. When a mobile
     viewport shrinks in place (390 -> 320, or a rotation), the previous px
     width briefly overflows; mobile browsers then zoom out to fit it, and
     visualViewport.width reports the stale wider value back. Clamping to
     documentElement.clientWidth, which content overflow cannot widen, breaks
     that loop so the page returns to scale 1 at the new width. */
    function readLiveViewportWidth(){var viewport=window.visualViewport,root=document.documentElement,width=viewport?viewport.width:window.innerWidth,layout=root?root.clientWidth:0;return layout>0?Math.min(width,layout):width;}
    function syncEffectiveViewport(){var viewport=window.visualViewport,width=readLiveViewportWidth(),narrow=width<=640;document.body.style.setProperty("--okf-effective-vw",Math.max(1,width)+"px");document.body.style.setProperty("--okf-effective-left",Math.max(0,viewport?viewport.offsetLeft:0)+"px");document.body.toggleAttribute("data-okf-narrow-reflow",narrow);syncCriticalMobileChrome(narrow);if(!narrow&&paneNav&&paneNavHome&&paneNavHome.parentNode)paneNavHome.parentNode.insertBefore(paneNav,paneNavHome.nextSibling);}
  syncEffectiveViewport();window.addEventListener("resize",syncEffectiveViewport);window.addEventListener("load",syncEffectiveViewport);window.addEventListener("pageshow",function(event){if(event.persisted)syncEffectiveViewport();});setTimeout(syncEffectiveViewport,0);setTimeout(syncEffectiveViewport,120);setTimeout(syncEffectiveViewport,500);if(window.visualViewport){window.visualViewport.addEventListener("resize",syncEffectiveViewport);window.visualViewport.addEventListener("scroll",syncEffectiveViewport);}
  var DATA_URL = (graphEl && graphEl.getAttribute("data-graph-url")) || window.OKF_LOOM_GRAPH_DATA_URL || null;
  var LIVE_GRAPH = DATA_URL === "/__data/graph.json" && document.body.dataset.okfMode === "serve";
  var GRAPH_REQUEST_URL = LIVE_GRAPH ? DATA_URL + "?view=structure" : DATA_URL;
  var cancelGraphBodies = null;
  var detailBodyPending = false, detailBodyError = null;
  var CONCEPT_PREFIX = (graphEl && graphEl.getAttribute("data-concept-prefix")) || window.OKF_CONCEPT_PAGE_PREFIX || "/";
  var INITIAL_LAYOUT = (graphEl && graphEl.getAttribute("data-initial-layout")) || window.OKF_LOOM_INITIAL_LAYOUT || "cose";
  // P1-3: build mode (serve|spa|static) is emitted on <body data-okf-mode>
  // by the graph_page template. In static mode the Open-page href needs a
  // .html suffix (concept pages are emitted as <id>.html; the bare path
  // 404s). Default to "serve" when the attribute is absent (single-file
    // viewers and older embedded pages).
  var MODE = (document.body && document.body.getAttribute("data-okf-mode")) || "serve";
  var IS_SINGLE = !!window.BUNDLE;
  var mobileModeIndicator=null,mobileModeHome=null;
  if(mobileSecondaryRow){var brand=document.querySelector(".okf-topbar__brand"),studioSummarizesLive=MODE==="serve"&&document.querySelector('[data-okf-studio-capability="available"]');mobileModeIndicator=MODE==="serve"?liveStatus:null;if(!mobileModeIndicator&&!studioSummarizesLive){mobileModeIndicator=document.createElement("span");mobileModeIndicator.setAttribute("role","status");}if(mobileModeIndicator){mobileModeIndicator.tabIndex=0;mobileModeIndicator.textContent=IS_SINGLE?"Local":(MODE==="static"?"Static":"Live");mobileModeIndicator.classList.add("okf-mobile-mode");if(brand){brand.appendChild(mobileModeIndicator);mobileModeHome=document.createComment("okf mode home");brand.insertBefore(mobileModeHome,mobileModeIndicator);}}syncEffectiveViewport();}
    var graphLayout = document.querySelector(".okf-layout");
    function isCompactGraphViewport(){return readLiveViewportWidth()<=900;}
  function setMobilePane(name, focusPane) {
    if (!graphLayout || ["graph", "concepts", "details"].indexOf(name) < 0) return;
    // Pane-local scrollports own traversal; retired list scroll must not move
    // the next pane underneath the shell.
    var previousPane = graphLayout.getAttribute("data-mobile-pane");
    if (previousPane !== name) graphLayout.scrollTop = 0;
    graphLayout.setAttribute("data-mobile-pane", name);
    Array.prototype.forEach.call(document.querySelectorAll("[data-graph-pane]"), function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-graph-pane") === name ? "true" : "false");
    });
    var index = document.querySelector(".okf-node-index");
    if (index && name === "concepts") index.open = true;
    else if (index && previousPane === "concepts") index.open = false;
    try{window.dispatchEvent(new CustomEvent("okf-loom:graph-pane",{detail:{pane:name}}));}catch(e){}
    if (focusPane && name === "details") {
      // A single-file document starts with no selected article, so its real
      // detail/document region is the aside rather than hidden #detail-content.
      var content = document.getElementById("detail-content");
      var detail = content && !content.hidden ? content : document.getElementById("okf-detail");
      if (detail) {
        detail.setAttribute("tabindex", "-1");
        window.requestAnimationFrame(function () {
          var owner = document.getElementById("okf-detail");
          if (owner) owner.scrollTop = 0;
          if (detail.isConnected) detail.focus({ preventScroll: true });
        });
      }
    }
  }
  if (graphLayout) {
    setMobilePane(isCompactGraphViewport() ? "concepts" : "graph", false);
    Array.prototype.forEach.call(document.querySelectorAll("[data-graph-pane]"), function (button) {
      button.addEventListener("click", function () { setMobilePane(button.getAttribute("data-graph-pane"), true); });
    });
  }
    var portableLoadGeneration = 0;
    var portableLoadController = null;
    var portableLoadPromise = null;
    var portableLoadError = null;
    var graphBundleRevision = null;
    // Single-file rendered-source binding: canonical concept identity ->
    // per-concept normalized-source revision, parsed ONCE from the snapshot's
    // canonical sibling script. Null on other surfaces or when the map is
    // absent/malformed/over-bound — a section intent then never binds.
    var singleSourceRevisions = null;
    var graphMountGeneration = 0;
    var graphPageActive = true;
    var graphRequestGeneration = 0;
    var graphInitialController = null;
    var graphRefreshController = null;
    var graphRefreshDebounceTimer = null;
    var GRAPH_CONTEXT_DEADLINE = Number((window.OKF_LOOM_TEST_DEADLINES || {}).context || 30000);
  function portableRequired() { return IS_SINGLE || MODE === "static" || MODE === "spa"; }
  function updateGraphContextAvailability() {
    var button = document.getElementById("okf-copy-neighbourhood");
    if (!button) return;
    var unavailable = portableRequired() && !window.okfPortableProvider;
    button.disabled = IS_SINGLE && unavailable;
    button.title = unavailable ? "Portable evidence loads when this context action is used" : "Copy exact cited context for the selected concept and its neighbours";
  }
    function clearPortableGraphFailure() { var old=document.querySelector(".okf-graph-context-unavailable");if(old)old.remove(); }
    function showPortableGraphFailure(error,target,targetGeneration,owner,options,timedOut) {
      var actions=document.getElementById("okf-graph-context-actions");if(!actions||!target)return null;
      clearPortableGraphFailure();var box=document.createElement("div");box.className="okf-graph-context-unavailable";box.dataset.contextConcept=target;box.dataset.selectionGeneration=String(targetGeneration);box.setAttribute("role","alert");
      var message=document.createElement("p");message.textContent=timedOut?"Neighbourhood context took longer than 30 seconds. Retry to start a fresh request.":"Neighbourhood context is unavailable. Retry, or use the available fallback.";
      var retry=document.createElement("button");retry.type="button";retry.className="okf-btn okf-btn--primary";retry.textContent="Retry";
      retry.addEventListener("click",function(){if(!window.okfGraphSelection||window.okfGraphSelection.conceptId!==target||window.okfGraphSelection.generation!==targetGeneration)return;copySelectedNeighbourhood(owner||document.getElementById("okf-copy-neighbourhood"),options||{neighbors:1,budget:6000},true);});
      var manual=document.createElement("a");manual.className="okf-btn okf-btn--quiet";
      if(MODE==="static"||MODE==="spa"){manual.textContent="Open emitted concept-only Markdown";manual.href="__context/"+target+".txt";}
      else if(IS_SINGLE){manual.textContent="Browse concepts while context is unavailable";manual.href="#okf-node-index-list";}
      else{manual.textContent="Open canonical Markdown context";manual.href="/__context?concept="+target+"&format=markdown";}
      box.appendChild(message);box.appendChild(retry);box.appendChild(manual);actions.appendChild(box);return retry;
    }
    function ensurePortableGraphProvider(force) {
      if (window.okfPortableProvider) {
        var currentRevision = String(window.okfPortableProvider.manifest && window.okfPortableProvider.manifest.bundle_revision || "");
          if (graphBundleRevision && (!currentRevision || graphBundleRevision !== currentRevision)) {
          teardownGraphLifecycle("provider-revision-mismatch", { invalidateProvider: true });
        } else {
          portableLoadError=null;clearPortableGraphFailure();updateGraphContextAvailability();return Promise.resolve(window.okfPortableProvider);
        }
      }
      if (!portableRequired()) { portableLoadError=null;clearPortableGraphFailure();updateGraphContextAvailability();return Promise.resolve(null); }
    if (!window.okfPortableRetrieval || IS_SINGLE) { updateGraphContextAvailability();return Promise.reject(new Error("Portable retrieval runtime is unavailable.")); }
    if(portableLoadPromise&&!force)return portableLoadPromise;
    if (portableLoadController) portableLoadController.abort();
    var controller = new AbortController(), own = ++portableLoadGeneration;
    portableLoadController = controller;
    var root = MODE === "static" ? "__data/" : "/__data/";
      portableLoadPromise=window.okfPortableRetrieval.loadStatic(root, { controller: controller, generation: own })
          .then(function (provider) {
            if (own !== portableLoadGeneration) throw new DOMException("Superseded","AbortError");
            var providerRevision = String(provider && provider.manifest && provider.manifest.bundle_revision || "");
              if (graphBundleRevision && (!providerRevision || graphBundleRevision !== providerRevision)) {
              var mismatch = new Error("Portable context revision does not match the visible graph. Retry to load current evidence.");
              teardownGraphLifecycle("provider-revision-mismatch", { invalidateProvider: true });
              portableLoadError = mismatch;
              showPortableGraphFailure(mismatch, selectedConceptId, selectionGeneration);
              throw mismatch;
            }
            portableLoadError=null;window.okfPortableProvider=provider;clearPortableGraphFailure();updateGraphContextAvailability();window.dispatchEvent(new CustomEvent("okf-loom:portable-ready",{detail:{revision:provider.manifest.bundle_revision}}));return provider;
          })
        .catch(function (error) {if(own===portableLoadGeneration){portableLoadError=error;window.okfPortableProvider=null;showPortableGraphFailure(error,selectedConceptId,selectionGeneration);updateGraphContextAvailability();}throw error;})
      .finally(function(){if(own===portableLoadGeneration)portableLoadPromise=null;});
    return portableLoadPromise;
  }
    window.addEventListener("pagehide",function(){
      graphPageActive = false;
      if (cancelGraphBodies) cancelGraphBodies();
      graphRequestGeneration++;
      if (DATA_URL === "/__data/graph.json") window.okfGraphData = null;
      if (graphInitialController) graphInitialController.abort();
      if (graphRefreshController) graphRefreshController.abort();
      if (graphRefreshDebounceTimer) clearTimeout(graphRefreshDebounceTimer);
      graphRefreshDebounceTimer = null;
      if (typeof window.__okfCancelGraphDeltas === "function") window.__okfCancelGraphDeltas("pagehide");
      teardownGraphLifecycle("pagehide", { invalidateProvider: true });
    });
    window.addEventListener("pageshow",function(event){
      if (!event.persisted) return;
      graphPageActive = true;
      if (typeof window.__okfRefreshGraph === "function") window.__okfRefreshGraph();
      else startInitialAcquisition();
    });
  window.addEventListener("okf-loom:portable-ready", updateGraphContextAvailability);

  // Stable selected-node ownership seam. Consumers read the frozen snapshot
  // or subscribe to okf-loom:graph-selection; clear always publishes null.
  var selectedConceptId = null;
  var selectionGeneration = 0;
  var latestGraphInteractionSnapshot = null;
    var graphContextController = null;
    var graphCopyGeneration = 0;
      function teardownGraphLifecycle(reason, options) {
      options = options || {};
      graphCopyGeneration++;
      portableLoadGeneration++;
      if (graphContextController) graphContextController.abort();
      if (portableLoadController) portableLoadController.abort();
      graphContextController = null;
      portableLoadController = null;
      portableLoadPromise = null;
      clearPortableGraphFailure();
        var fallback = document.getElementById("okf-graph-copy-fallback");
        if (fallback) fallback.hidden = true;
        var fallbackText = document.getElementById("okf-graph-copy-text"); if (fallbackText) fallbackText.value = "";
        var status = document.getElementById("okf-graph-copy-status"); if (status) { status.textContent = ""; status.setAttribute("role", "status"); }
      var copy = document.getElementById("okf-copy-neighbourhood");
      if (copy) {
        copy.removeAttribute("aria-busy");
        copy.textContent = copy.getAttribute("data-copy-label") || "Copy concept context";
      }
      if (options.invalidateProvider) window.okfPortableProvider = null;
      updateGraphContextAvailability();
      return reason;
    }
    window.okfTeardownGraphLifecycle = teardownGraphLifecycle;
  try {
    Object.defineProperty(window, "okfGraphSelection", {
      configurable: true,
      get: function () { return Object.freeze({ conceptId: latestGraphInteractionSnapshot ? latestGraphInteractionSnapshot.selected : selectedConceptId, generation: selectionGeneration }); }
    });
  } catch (e) {}
    function publishGraphSelection(conceptId, source) {
      teardownGraphLifecycle(conceptId ? "selection-rebind" : "selection-clear", { invalidateProvider: false });
      detailBodyPending = false; detailBodyError = null;
      selectedConceptId = conceptId || null;
      if (latestGraphInteractionSnapshot) latestGraphInteractionSnapshot = Object.freeze(Object.assign({}, latestGraphInteractionSnapshot, { selected: selectedConceptId }));
    selectionGeneration++;
    graphCopyGeneration++;
      if (graphContextController) graphContextController.abort();
      clearPortableGraphFailure();
      var actions = document.getElementById("okf-graph-context-actions");
      if (actions) actions.hidden = !selectedConceptId;
    var copyButton = document.getElementById("okf-copy-neighbourhood");
      if (copyButton && copyButton.getAttribute("data-copy-label")) copyButton.textContent = copyButton.getAttribute("data-copy-label");
      if (selectedConceptId && portableLoadError && !window.okfPortableProvider) showPortableGraphFailure(portableLoadError,selectedConceptId,selectionGeneration);
    updateGraphContextAvailability();
    try {
      window.dispatchEvent(new CustomEvent("okf-loom:graph-selection", {
        detail: { conceptId: selectedConceptId, generation: selectionGeneration, source: source || "graph" }
      }));
    } catch (e) {}
  }
  function graphCopyStatus(message, assertive) {
    var status = document.getElementById("okf-graph-copy-status");
    if (status) {
      status.setAttribute("role", assertive ? "alert" : "status");
      status.setAttribute("aria-live", assertive ? "assertive" : "polite");
      status.setAttribute("aria-atomic", "true");
      status.textContent = (assertive ? "Error: " : "") + message;
    }
  }
  function showGraphManualCopy(textValue) {
    var wrap = document.getElementById("okf-graph-copy-fallback");
    var area = document.getElementById("okf-graph-copy-text");
    if (!wrap || !area) return;
    area.value = textValue; wrap.hidden = false;
    graphCopyStatus("Automatic copy was unavailable. Exact neighbourhood context is ready for manual copy.", true);
  }
  function copyGraphText(textValue, owner, own) {
    var label = owner.getAttribute("data-copy-label") || owner.textContent;
    owner.textContent = "Copying";
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      owner.textContent = label; showGraphManualCopy(textValue); return Promise.resolve(false);
    }
    return new Promise(function (resolve) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return; settled = true;
        if (own === graphCopyGeneration) { owner.textContent = label; showGraphManualCopy(textValue); }
        resolve(false);
      }, 5000);
      Promise.resolve(navigator.clipboard.writeText(textValue)).then(function () {
        if (settled) return; settled = true; clearTimeout(timer);
        if (own === graphCopyGeneration && owner.isConnected) {
          owner.textContent = "Copied"; graphCopyStatus("Copied concept context.", false);
          setTimeout(function () { if (own === graphCopyGeneration && owner.isConnected) owner.textContent = label; }, 1600);
        }
        resolve(true);
      }, function () {
        if (settled) return; settled = true; clearTimeout(timer);
        if (own === graphCopyGeneration) { owner.textContent = label; showGraphManualCopy(textValue); }
        resolve(false);
      });
    });
  }
      function copySelectedNeighbourhood(owner, options, retrying) {
        var snapshot = window.okfGraphSelection;
        if (!snapshot || !snapshot.conceptId) return Promise.resolve(false);
        teardownGraphLifecycle("clipboard-start", { invalidateProvider: false });
        var own = ++graphCopyGeneration;
        var concept = snapshot.conceptId, selectionOwn=snapshot.generation; options = options || {};
        owner.setAttribute("data-copy-label", owner.getAttribute("data-copy-label") || owner.textContent);
        owner.textContent = "Preparing"; owner.setAttribute("aria-busy","true"); graphCopyStatus("Preparing neighbourhood context.", false);
        var localController = new AbortController(); graphContextController = localController; var timedOut=false, timer;
        var actionStartedAt=Date.now(), actionDeadline=actionStartedAt+GRAPH_CONTEXT_DEADLINE;
        function requireBeforeDeadline(){if(Date.now()>=actionDeadline){timedOut=true;localController.abort();if(portableLoadController)portableLoadController.abort();throw new DOMException("Graph context deadline exceeded","TimeoutError");}}
        var deadline=new Promise(function(_,reject){timer=setTimeout(function(){timedOut=true;localController.abort();if(portableLoadController)portableLoadController.abort();reject(new DOMException("Graph context deadline exceeded","TimeoutError"));},GRAPH_CONTEXT_DEADLINE);});
        function current(){return own===graphCopyGeneration&&window.okfGraphSelection&&window.okfGraphSelection.conceptId===concept&&window.okfGraphSelection.generation===selectionOwn;}
        var work=(portableRequired()?ensurePortableGraphProvider(false):Promise.resolve(null)).then(function(provider){
          requireBeforeDeadline();
          if(!current())throw new DOMException("Superseded","AbortError");
          if(provider)return Promise.resolve().then(function(){requireBeforeDeadline();var pack=provider.context({concept:concept},{mode:"lexical",budget:options.budget||6000,neighbors:options.neighbors==null?1:options.neighbors,limit:20},[]);requireBeforeDeadline();return pack;});
          var params=new URLSearchParams({neighbors:String(options.neighbors==null?1:options.neighbors),budget_tokens:String(options.budget||6000),limit:"20",format:"json"});
          return fetch("/__context?concept="+concept+"&"+params.toString(),{signal:localController.signal,headers:{Accept:"application/json"}}).then(function(response){if(!response.ok)throw new Error("Context request failed ("+response.status+")");return response.json();}).then(function(pack){requireBeforeDeadline();return pack;});
        });
        return Promise.race([work,deadline])
        .then(function (pack) {
          requireBeforeDeadline();
          if (!current()) return false;
          if(!pack.representations||typeof pack.representations.markdown!=="string")throw new Error("Context response did not include canonical Markdown.");
            if(graphBundleRevision&&(!pack.bundle_revision||pack.bundle_revision!==graphBundleRevision))throw new Error("Context revision does not match the visible graph.");
          requireBeforeDeadline();
          graphCopyStatus("Prepared "+pack.included+" passages across "+(new Set(pack.items.map(function(i){return i.concept_id;}))).size+" concepts for "+concept+", "+pack.neighbors+" hop"+(pack.neighbors===1?"":"s")+", "+pack.budget.estimated_tokens+" estimated tokens"+(pack.truncated?", truncated.":"."),false);
          owner.removeAttribute("aria-busy"); if(retrying)owner.focus();
          requireBeforeDeadline();
          return copyGraphText(pack.representations.markdown, owner, own);
        }).catch(function (error) {
          if (!current() || error.name==="AbortError"&&!timedOut) return false;
          owner.textContent = owner.getAttribute("data-copy-label")||"Copy concept context";owner.removeAttribute("aria-busy");
          graphCopyStatus(timedOut||error.name==="TimeoutError"?"Neighbourhood context took longer than 30 seconds.":"Neighbourhood context is unavailable.", true);
          var retry=showPortableGraphFailure(error,concept,selectionOwn,owner,options,timedOut||error.name==="TimeoutError");if(retry)retry.focus();
          return false;
        }).finally(function () { clearTimeout(timer); });
  }
  window.okfGraphContextCopy = copySelectedNeighbourhood;
  var graphCopyButton = document.getElementById("okf-copy-neighbourhood");
  if (graphCopyButton) graphCopyButton.addEventListener("click", function () { copySelectedNeighbourhood(graphCopyButton, { neighbors: 1, budget: 6000 }); });
  var graphCopySelect = document.getElementById("okf-graph-copy-select");
  if (graphCopySelect) graphCopySelect.addEventListener("click", function () { var area = document.getElementById("okf-graph-copy-text"); if (area) { area.focus(); area.select(); } });

  // Theme state is owned by theme.js; graph.js is a read-only consumer
  // (graphPalette resolves the active data-theme; init subscribes to
  // okf-loom:themeChanged to re-sync the canvas).
  var reducedMotionQuery = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
  var REDUCED_MOTION = !!(reducedMotionQuery && reducedMotionQuery.matches);
  var forcedColorsQuery=window.matchMedia&&window.matchMedia("(forced-colors: active)"),FORCED_COLORS=!!(forcedColorsQuery&&forcedColorsQuery.matches);
  if(reducedMotionQuery){var syncReducedMotion=function(event){REDUCED_MOTION=!!event.matches;document.body.toggleAttribute("data-okf-reduced-motion",REDUCED_MOTION);};if(reducedMotionQuery.addEventListener)reducedMotionQuery.addEventListener("change",syncReducedMotion);else if(reducedMotionQuery.addListener)reducedMotionQuery.addListener(syncReducedMotion);document.body.toggleAttribute("data-okf-reduced-motion",REDUCED_MOTION);}

  // Layout parameters tuned for spread. The previous default
  // ({ name: 'cose', padding: 30 }) packed nodes too tightly — users
  // complained about everything being bunch up together. cose has many
  // spread knobs; this preset increases node repulsion, ideal edge
  // length, component spacing, and padding so the graph breathes.
  //
  // Returns the full layout options object for the given layout name.
  // Pass `randomize` for an initial layout (true) or a re-layout /
  // incremental add (false) — same as the original `randomize` semantics.
  //
  // Signal controls: every spacing/spread knob now accepts an
  // override via `opts` so the Signal-controls panel can drive node spacing,
  // cluster separation, and group strength from the UI. When an override is
  // absent the original tuned defaults apply, so behaviour is unchanged for
  // any caller that does not pass tuning (single-file viewer, presence focus).
  // `opts.idealEdgeLength` MAY be a function(edge) — that is how grouping pulls
  // same-group nodes together and pushes different-group nodes apart on cose.
  // Phase 3: prefer fCoSE (the constraint-capable successor to cose from
  // the same authors) whenever its CDN chain loaded. It spreads clusters
  // dramatically better, packs disconnected components, and TILES
  // degree-zero nodes into a tray instead of letting them drift (the
  // orphan shelf). cytoscape-fcose self-registers against
  // window.cytoscape; the global is our capability probe. Offline /
  // cdn:false keeps plain cose (identical option names, softer results).
  function fcoseAvailable() {
    return typeof window.cytoscapeFcose !== "undefined";
  }

  function layoutOpts(name, opts) {
    opts = opts || {};
    var randomize = opts.randomize !== false;
    var animate = REDUCED_MOTION ? false : (opts.animate === undefined ? true : opts.animate);
    var pad = opts.padding || 60;
    // fit defaults to FALSE: graph.js owns fitting via overlayAwareFit() so it
    // can respect a MAX_ZOOM clamp + overlay insets (reviewer over-zoom blocker).
    var common = { name: name, animate: animate, padding: pad, fit: opts.fit === true };
    if (opts.animationDuration != null) common.animationDuration = opts.animationDuration;
    if (opts.animationEasing) common.animationEasing = opts.animationEasing;
    if (name === "cose" && fcoseAvailable()) {
      // The Signal-controls overrides map 1:1 (fcose shares cose's knob
      // names for repulsion/ideal-length/elasticity/gravity).
      return Object.assign(common, {
        name: "fcose",
        quality: "default",
        randomize: randomize,
        nodeRepulsion: opts.nodeRepulsion || 12000,
        idealEdgeLength: opts.idealEdgeLength || 120,
        edgeElasticity: opts.edgeElasticity || 0.45,
        gravity: opts.gravity != null ? opts.gravity : 0.25,
        gravityRange: 3.8,
        numIter: opts.numIter || 2500,
        nodeSeparation: 110,
        packComponents: true,
        tile: true,
      });
    }
    if (name === "cose") {
      // Spread tuned for ~30-500 node bundles. Higher repulsion + longer
      // ideal edges push clusters apart; component spacing adds breathing
      // room around disconnected subgraphs; more iterations let the
      // force-directed layout converge to a less-jittery stable state.
      return Object.assign(common, {
        randomize: randomize,
        nodeRepulsion: opts.nodeRepulsion || 12000,
        // nodeOverlap is cose's OVERLAPPING-node repulsion — the lever that
        // actually stops same-cluster nodes piling on top of each other (the
        // reviewer-1 collision). Default 4 is far too weak for our node sizes.
        nodeOverlap: opts.nodeOverlap || 24,
        idealEdgeLength: opts.idealEdgeLength || 140,
        edgeElasticity: opts.edgeElasticity || 0.45,
        gravity: opts.gravity != null ? opts.gravity : 0.25,
        numIter: opts.numIter || 3500,
        componentSpacing: opts.componentSpacing || 100,
        nestingFactor: 5,
        coolingFactor: 0.95,
        minTemp: 1.0,
      });
    }
    if (name === "dagre" && typeof window.dagre !== "undefined") {
      // Layered/Sugiyama layout for the Flow lens: reading direction =
      // dependency direction (left → right). The evidence-backed layout
      // for directed data (yFiles/Linkurious both ship it as the
      // dedicated "flow" layout).
      return Object.assign(common, {
        name: "dagre",
        rankDir: "LR",
        nodeSep: 26 + 24 * (opts.spacingFactor || 1),
        rankSep: 70 + 50 * (opts.spacingFactor || 1),
        edgeSep: 12,
      });
    }
    if (name === "dagre" || name === "breadthfirst") {
      // dagre requested but its CDN chain is absent → directed
      // breadthfirst approximates the layering.
      return Object.assign(common, {
        name: "breadthfirst",
        directed: name === "dagre" ? true : false,
        spacingFactor: opts.spacingFactor || 1.4,
        padding: pad,
      });
    }
    if (name === "concentric") {
      return Object.assign(common, {
        concentric: opts.concentric || function (n) { return n.degree(); },
        levelWidth: function () { return 2; },
        spacingFactor: opts.spacingFactor || 1.3,
        minNodeSpacing: opts.minNodeSpacing || 40,
        padding: pad,
      });
    }
    if (name === "circle") {
      return Object.assign(common, { radius: opts.radius || 280, padding: pad, spacingFactor: opts.spacingFactor || 1 });
    }
    if (name === "grid") {
      return Object.assign(common, { sort: opts.sort, padding: pad, avoidOverlap: true, spacingFactor: opts.spacingFactor || 1.25 });
    }
    // Fallback for any custom layout: hand back the common options.
    return common;
  }

  // applyTheme() and theme storage ownership REMOVED — owned by theme.js.
  // This file only READS data-theme via graphPalette() and re-syncs the
  // canvas when theme.js dispatches `okf-loom:themeChanged` (see init).

  // Content types are not colour-coded (Ledger), so the graph has no type
  // chip fills and needs no luminance-picked chip foreground.
  // Theme boot/cycle ownership REMOVED — theme.js resolves the saved
  // preference + Auto/OS and owns the Appearance control. graph.js only
  // re-syncs the canvas when the resolved theme changes (see init).

  // ---- Bundle acquisition ------------------------------------------------
  function endpointKey(source,target){return JSON.stringify([source,target]);}
  function legacyEdgeId(source,target){return "legacy-edge:"+source.length+":"+source+":"+target.length+":"+target;}
  function runtimeEdgeId(data){return data.id||legacyEdgeId(data.source,data.target);}
  function graphCandidate(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw Object.assign(new Error("Graph payload must be an object."), { okfPhase: "graph-schema" });
    var revision = raw.bundle_revision || raw.revision;
    if (typeof revision !== "string" || !revision.trim()) throw Object.assign(new Error("Graph payload revision is required."), { okfPhase: "graph-schema" });
    if (!Array.isArray(raw.nodes) || !Array.isArray(raw.edges)) throw Object.assign(new Error("Graph payload nodes and edges must be arrays."), { okfPhase: "graph-schema" });
    var ids = {}, nodes = raw.nodes.slice(), edges = raw.edges.slice(), outgoing = {}, backlinks = {};
    nodes.forEach(function (node) {
      var data = node && node.data, id = data && data.id;
      if (typeof id !== "string" || !id.trim() || ids[id]) throw Object.assign(new Error("Graph node IDs must be unique non-empty strings."), { okfPhase: "graph-schema" });
      // Property presence is the availability contract: an omitted count is
      // unknown (not zero), while an explicitly supplied zero is valid.
      if (Object.prototype.hasOwnProperty.call(data, "active_comment_count") &&
          (!Number.isInteger(data.active_comment_count) || data.active_comment_count < 0)) {
        throw Object.assign(new Error("Graph comment counts must be non-negative integers."), { okfPhase: "graph-schema" });
      }
      ids[id] = true;
    });
    var edgeIds={}, edgePairs={};
    edges.forEach(function (edge, edgeIndex) {
      var data = edge && edge.data, source = data && data.source, target = data && data.target;
      if (raw.structure_schema === 1 && data) {
        data = Object.assign({}, data);
        edge = edges[edgeIndex] = { data: data };
        if (!Array.isArray(data.relations) || !Number.isInteger(data.primary_relation_index) || data.primary_relation_index < 0 || data.primary_relation_index >= data.relations.length) throw Object.assign(new Error("Graph primary relation index is invalid."), { okfPhase: "graph-schema" });
        data.relations = data.relations.map(function (relation) { return Object.assign({}, relation, { source: source, target: target, direction: "outgoing" }); });
        data.primary_relation = data.relations[data.primary_relation_index];
        delete data.primary_relation_index;
      }
      if (typeof source !== "string" || typeof target !== "string" || !ids[source] || !ids[target]) throw Object.assign(new Error("Graph edge endpoints must reference current nodes."), { okfPhase: "graph-schema" });
      if(data.id==null)data.id=legacyEdgeId(source,target);
      var pairKey=endpointKey(source,target);
      if(typeof data.id!=="string"||!data.id.trim()||edgeIds[data.id]||edgePairs[pairKey]) throw Object.assign(new Error("Graph bundled edge IDs and endpoint pairs must be unique."),{okfPhase:"graph-schema"});
      edgeIds[data.id]=true;edgePairs[pairKey]=true;
      // Backward compatibility: pre-bundle payloads become one-instance
      // bundles. New payloads fail closed if multiplicity or instance
      // direction/type metadata is inconsistent.
      if (data.relations == null) data.relations = [{ id: data.id + "__relation", label: data.label || "related", type: data.label || "link", kind: data.label ? "relation" : "body_link", direction: "outgoing", source: source, target: target, target_raw: target }];
      var relationIds={};
      if (!Array.isArray(data.relations) || !data.relations.length || data.relations.some(function (relation) { var invalid=!relation||typeof relation.id!=="string"||!relation.id.trim()||relationIds[relation.id]||typeof relation.label!=="string"||!relation.label.trim()||typeof relation.type!=="string"||!relation.type.trim()||(relation.kind!=="relation"&&relation.kind!=="body_link")||relation.direction!=="outgoing"||relation.source!==source||relation.target!==target; if(!invalid)relationIds[relation.id]=true;return invalid; })) throw Object.assign(new Error("Graph bundled edge relations are invalid."), { okfPhase: "graph-schema" });
      if (data.multiplicity == null) data.multiplicity = data.relations.length;
      if (!Number.isInteger(data.multiplicity) || data.multiplicity !== data.relations.length) throw Object.assign(new Error("Graph edge multiplicity must match its relation list."), { okfPhase: "graph-schema" });
      if (!data.primary_relation) data.primary_relation = data.relations[0];
      if(!data.primary_relation||!relationIds[data.primary_relation.id]||JSON.stringify(data.primary_relation)!==JSON.stringify(data.relations.filter(function(relation){return relation.id===data.primary_relation.id;})[0])) throw Object.assign(new Error("Graph primary relation must be an exact member of its relation list."),{okfPhase:"graph-schema"});
      (outgoing[source] = outgoing[source] || []).push(target);
      (backlinks[target] = backlinks[target] || []).push(source);
    });
    ["outgoing", "backlinks"].forEach(function (name) {
      var supplied = raw[name];
      if (supplied == null) return;
      if (typeof supplied !== "object" || Array.isArray(supplied)) throw Object.assign(new Error("Graph maps must be objects."), { okfPhase: "graph-schema" });
      Object.keys(supplied).forEach(function (id) {
        if (!ids[id] || !Array.isArray(supplied[id]) || supplied[id].some(function (other) { return typeof other !== "string" || !ids[other]; })) throw Object.assign(new Error("Graph maps must contain current node IDs."), { okfPhase: "graph-schema" });
      });
    });
    var candidate = Object.assign({}, raw, { bundle_revision: revision.trim(), nodes: nodes, edges: edges, outgoing: outgoing, backlinks: backlinks });
    delete candidate.structure_schema;
    return candidate;
  }

  function publishGraphTerminal(code, phase, error) {
    document.body.dataset.okfGraphCaptureReady = code;
    document.body.dataset.okfCaptureReady = code;
    window.okfGraphCaptureState = function () { return { initialized: false, nodeCount: 0, payloadNodeCount: 0, layoutRequired: false, layoutSettled: false, layoutGeneration: 0, appliedLayoutGeneration: 0, terminalCode: code, terminalPhase: phase, terminalMessage: String(error && error.message || error || "Graph unavailable") }; };
  }

  function acquireBundle(controller) {
    if (window.BUNDLE) return Promise.resolve(window.BUNDLE);
    if (!DATA_URL) return Promise.reject(new Error("no bundle data"));
    // Live graph and Studio share initial acquisition, regardless of script
    // order. Portable DATA_URL/window.BUNDLE paths retain their own transport.
    if (DATA_URL === "/__data/graph.json" && window.okfGraphData) return window.okfGraphData;
    var acquisition = fetch(GRAPH_REQUEST_URL, { signal: controller.signal, cache: "no-cache", headers: { Accept: "application/json" } }).catch(function (error) {
      if (error && error.name === "AbortError") throw error;
      throw Object.assign(new Error("Graph data request failed."), { okfPhase: "graph-fetch", okfCode: "network-failure", cause: error });
    }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return null; }).then(function (data) {
        throw Object.assign(new Error(data && data.error && data.error.message || "HTTP " + r.status), { okfPhase: "graph-http", okfCode: "network-failure" });
      });
      return r.json().catch(function (error) { throw Object.assign(new Error("Graph JSON is malformed."), { okfPhase: "graph-json", okfCode: "network-failure", cause: error }); });
    });
    if (DATA_URL === "/__data/graph.json") {
      window.okfGraphData = acquisition;
      acquisition.catch(function () { if (window.okfGraphData === acquisition) window.okfGraphData = null; });
    }
    return acquisition;
  }

  function startInitialAcquisition() {
    if (graphInitialController) graphInitialController.abort();
    var own = ++graphRequestGeneration;
    var controller = new AbortController();
    graphInitialController = controller;
    return acquireBundle(controller).then(function (bundle) {
      if (!graphPageActive || controller.signal.aborted || own !== graphRequestGeneration) return;
      bundle = graphCandidate(bundle);
      init(bundle);
      if (DATA_URL === "/__data/graph.json") {
        window.okfGraphData = Promise.resolve(bundle);
        window.dispatchEvent(new CustomEvent("okf-loom:graph-data", { detail: bundle }));
      }
    }).catch(function (err) {
      if (!graphPageActive || controller.signal.aborted || own !== graphRequestGeneration || (err && err.name === "AbortError")) return;
    // Surface the stack in the console — a swallowed init error is
    // undebuggable from the error banner alone.
      try { console.error("[okf-graph] init failed:", err && err.stack ? err.stack : err); } catch (e2) {}
      publishGraphTerminal(err && err.okfCode || "unavailable", err && err.okfPhase || "graph-init", err);
      showLoadError(err && err.message ? err.message : String(err));
    }).finally(function () { if (graphInitialController === controller) graphInitialController = null; });
  }
  startInitialAcquisition();

  function showLoadError(msg) {
    var el = document.getElementById("okf-graph");
    if (!el) return;
    var notice = document.createElement("div");
    notice.className = "okf-graph-loading okf-graph-loading--error";
    notice.textContent = "Graph failed to load: " + msg;
    el.appendChild(notice);
    var announcer=document.getElementById("okf-graph-announcer");if(announcer)announcer.textContent="Error: Graph failed to load. "+msg;
  }

    function showWaiting(msg) {
    var el = document.getElementById("okf-graph");
    if (!el) return;
    var notice = document.createElement("div");
    notice.className = "okf-graph-loading";
    notice.textContent = msg;
    el.appendChild(notice);
      return notice;
    }

    // Graph owns its subsystem signal, but the page-wide signal is shared with
    // embedded retrieval on single-file pages. A terminal unavailable result
    // from that earlier owner must not be promoted by a later graph layout.
    // Last coherent readiness belongs to an accepted graph payload/layout, not
    // to mutable body attributes. A superseding refresh can start while those
    // attributes say "loading"; its failure must restore this accepted state.
    var lastAcceptedCaptureReadiness = {
      graph: document.body.dataset.okfGraphCaptureReady || "loading",
      page: document.body.dataset.okfCaptureReady || "loading"
    };
    var graphReadyPublicationCount = 0;
    function publishGraphCaptureReady(state, acceptCoherentState) {
      var pageWillChange=state==="unavailable"||document.body.dataset.okfCaptureReady!=="unavailable"&&document.body.dataset.okfCaptureReady!==state;
      var publicationChangesState=document.body.dataset.okfGraphCaptureReady!==state||pageWillChange;
      if (state === "ready" && publicationChangesState) graphReadyPublicationCount++;
      document.body.dataset.okfGraphCaptureReady = state;
      if (state === "unavailable" || document.body.dataset.okfCaptureReady !== "unavailable") {
        document.body.dataset.okfCaptureReady = state;
      }
      if (acceptCoherentState) {
        lastAcceptedCaptureReadiness = {
          graph: document.body.dataset.okfGraphCaptureReady,
          page: document.body.dataset.okfCaptureReady
        };
      }
    }

    function init(bundle) {
      // Viewport reads can flush layout after chrome writes. The viewport
      // cannot resize during this synchronous init task; later fits read live.
      var initialMobileViewport = readMobileGraphViewport();
      if (cancelGraphBodies) cancelGraphBodies();
      if (graphMountGeneration++) teardownGraphLifecycle("graph-remount", { invalidateProvider: true });
      graphBundleRevision = String(bundle.bundle_revision || bundle.revision || "");
      singleSourceRevisions =
        (typeof window.okfPortableRetrieval === "object"
         && window.okfPortableRetrieval
         && typeof window.okfPortableRetrieval.parseSourceRevisions === "function")
        ? window.okfPortableRetrieval.parseSourceRevisions(
            (document.getElementById("okf-single-source-revisions") || {}).textContent || "")
        : null;
      // Static/portable payloads historically carried the directory slug
      // while serve mode received the configured display title.  Promote a
      // slug to the same product-facing human form instead of exposing an
      // implementation filename in one runtime only.
      var brandTitle=document.querySelector(".okf-topbar__brand strong"),payloadName=String(bundle.name||"");
      if(brandTitle&&/^[a-z0-9_-]+$/.test(payloadName)){brandTitle.textContent="OKF "+payloadName.replace(/[_-]+/g," ").replace(/\b\w/g,function(c){return c.toUpperCase();});}
      if (typeof window.cytoscape !== "function") {
        publishGraphCaptureReady("unavailable", true);
      window.okfGraphCaptureState = function () { return { initialized: true, nodeCount: 0, payloadNodeCount: Array.isArray(bundle.nodes) ? bundle.nodes.length : 0, layoutRequired: false, layoutSettled: true, layoutGeneration: 0, appliedLayoutGeneration: 0, terminalCode: "unavailable", terminalPhase: "graph-cytoscape" }; };
      showLoadError("Interactive graph unavailable because Cytoscape.js could not load. Search and Context remain available; use the readable concept relationship list below.");
      var fallback=document.createElement("div");fallback.className="okf-graph-fallback";var title=document.createElement("h2");title.textContent="Concept relationships";fallback.appendChild(title);var list=document.createElement("ul");(bundle.nodes||[]).slice().sort(function(a,b){return String(a.data.label||a.data.id).localeCompare(String(b.data.label||b.data.id));}).forEach(function(n){var li=document.createElement("li");li.textContent=(n.data.label||n.data.id)+" — "+((bundle.outgoing&&bundle.outgoing[n.data.id])||[]).length+" outgoing, "+((bundle.backlinks&&bundle.backlinks[n.data.id])||[]).length+" incoming";list.appendChild(li);});fallback.appendChild(list);var detail=document.getElementById("okf-detail");if(detail)detail.appendChild(fallback);
      return;
    }

    var layoutName = INITIAL_LAYOUT;
    // Sync the layout dropdown.
    // The raw layout dropdown is gone — each lens owns its
    // layout. The variable stays for the LOD grid switch below.
    var layoutSel = document.getElementById("okf-layout");

      var palette = bundle.palette || {};
      var bodies = bundle.bodies || {};
      // Serve-only lazy HTML. Two active reads, one latest queued selection,
      // 8 MiB / 32-entry LRU per accepted graph, and a 10 s read deadline.
      // Abort never releases admission until the underlying promise settles.
      var bodyEpoch = 0, bodyActive = 0, bodyQueue = [], bodyControllers = new Set();
      var bodyCache = new Map(), bodyCacheBytes = 0, bodyFlights = new Map();
      cancelGraphBodies = function () {
        bodyEpoch++; detailBodyPending = false; detailBodyError = null;
        bodyControllers.forEach(function (controller) { controller.abort(); });
        bodyQueue.splice(0).forEach(function (job) { job.reject(new DOMException("Superseded", "AbortError")); });
        bodyCache.clear(); bodyCacheBytes = 0; bodyFlights.clear();
      };
      function pumpGraphBodies() {
        while (bodyActive < 2 && bodyQueue.length) startGraphBody(bodyQueue.shift());
      }
      function startGraphBody(job) {
        bodyActive++;
        var controller = new AbortController(); bodyControllers.add(controller);
        var timer = setTimeout(function () { controller.abort(); }, Number((window.OKF_LOOM_TEST_DEADLINES || {}).graphBody || 10000));
        fetch("/__data/graph-body?id=" + encodeURIComponent(job.id) + "&revision=" + encodeURIComponent(job.revision),
          { signal: controller.signal, cache: "no-cache", headers: { Accept: "application/json" } })
          .then(function (response) { if (!response.ok) { var error = new Error("Concept body unavailable."); error.status = response.status; throw error; } return response.json(); })
          .then(function (doc) {
            if (controller.signal.aborted || job.epoch !== bodyEpoch) throw new DOMException("Superseded", "AbortError");
            if (!doc || doc.id !== job.id || doc.bundle_revision !== job.revision || typeof doc.html !== "string" || typeof doc.metadata_html !== "string") throw new Error("Concept body revision is invalid.");
            var bytes = 2 * (doc.html.length + doc.metadata_html.length);
            if (bytes <= 8 * 1024 * 1024) {
              bodyCache.set(job.id, { doc: doc, bytes: bytes }); bodyCacheBytes += bytes;
              while (bodyCache.size > 32 || bodyCacheBytes > 8 * 1024 * 1024) {
                var oldest = bodyCache.keys().next().value;
                bodyCacheBytes -= bodyCache.get(oldest).bytes; bodyCache.delete(oldest);
              }
            }
            job.resolve(doc);
          }).catch(job.reject).finally(function () {
            clearTimeout(timer); bodyControllers.delete(controller); bodyActive--;
            if (bodyFlights.get(job.id) === job.promise) bodyFlights.delete(job.id);
            pumpGraphBodies();
          });
      }
      function readGraphBody(id) {
        if (bodyCache.has(id)) {
          var entry = bodyCache.get(id); bodyCache.delete(id); bodyCache.set(id, entry);
          return Promise.resolve(entry.doc);
        }
        if (bodyFlights.has(id)) return bodyFlights.get(id);
        bodyQueue.splice(0).forEach(function (job) { job.reject(new DOMException("Superseded", "AbortError")); bodyFlights.delete(job.id); });
        var job = { id: id, revision: graphBundleRevision, epoch: bodyEpoch };
        job.promise = new Promise(function (resolve, reject) { job.resolve = resolve; job.reject = reject; });
        bodyFlights.set(id, job.promise); bodyQueue.push(job); pumpGraphBodies();
        return job.promise;
      }
      // Reserved documents (root/directory indexes, logs): {document, html}
      // reading records. They are never nodes and never selectable.
      var reservedDocuments = graphDocuments(bundle);
      function graphDocuments(candidate) {
        var documents = candidate.documents || {};
        (candidate.document_paths || []).forEach(function (path) { documents[path] = { document: path }; });
        return documents;
      }
      var external = bundle.external || [];
      var conceptPagePrefix = CONCEPT_PREFIX;

      // P1 graph/type collision fix: build a type → icon-key map from the
      // node data so the legend's "Filter types" section can show the type
      // ICON (non-colour encoding) instead of a colour swatch that collides
      // with the theme palette. Each node carries n.data.icon (the icon key);
      // the first node of each type wins.
      var typeIconKeys = {};
      (bundle.nodes || []).forEach(function (n) {
        var t = n.data.type;
        if (t && n.data.icon && !(t in typeIconKeys)) typeIconKeys[t] = n.data.icon;
      });
      // Render a type icon as an inline SVG using currentColor (inherits text
      // colour) so it is visible on any theme background and in forced-colors.
      function typeIconSvgMarkup(typeName) {
        var key = typeIconKeys[typeName] || "file";
        var inner = ICON_PATHS[key] || ICON_PATHS.file || "";
        return '<svg class="okf-graph-legend__type-icon" width="14" height="14" viewBox="0 0 24 24" '
          + 'fill="none" stroke="currentColor" stroke-width="1.8" '
          + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
          + inner + '</svg>';
      }

    // Type filter dropdown.
    var typeSel = document.getElementById("okf-filter-type");
    (bundle.types || []).forEach(function (t) {
      if (!typeSel) return;
      var opt = document.createElement("option");
      opt.value = t; opt.textContent = t;
      typeSel.appendChild(opt);
    });

    // Lookup tables.
    var nodeIndex = {};
    bundle.nodes.forEach(function (n) { nodeIndex[n.data.id] = n.data; });
    function metaValue(d, key) {
      if (!d) return "";
      if (d[key]) return String(d[key]);
      if (d.metadata && d.metadata[key]) return String(d.metadata[key]);
      return "";
    }
    function hasMetaValue(key) {
      return Object.keys(nodeIndex).some(function (id) { return !!metaValue(nodeIndex[id], key); });
    }

    // Backlinks map (target → [source,...]). Computed if absent.
    var backlinks = bundle.backlinks || {};
    if (!bundle.backlinks) {
      backlinks = {};
      bundle.edges.forEach(function (e) {
        var s = e.data.source, t = e.data.target;
        if (!t) return;
        (backlinks[t] = backlinks[t] || []);
        if (backlinks[t].indexOf(s) < 0) backlinks[t].push(s);
      });
    }
    // Outgoing map (source → [target,...]).
    var outgoing = {};
    bundle.edges.forEach(function (e) {
      var s = e.data.source, t = e.data.target;
      if (!t) return;
      (outgoing[s] = outgoing[s] || []);
      if (outgoing[s].indexOf(t) < 0) outgoing[s].push(t);
    });

    // iter2 CRI2-006: level-of-detail stopgap for large graphs. A
    // real catalog bundle easily hits 200-500 concepts; rendering every node
    // through a cose layout multi-second jank on first paint and pan/zoom
    // stays sluggish. The stopgap: when the bundle has more than
    // GRAPH_LOD_THRESHOLD nodes, render only the top-N by degree (the
    // highest-connectivity hubs, which carry the structure) and surface a
    // "Showing N of M nodes. Show all" pill. Clicking the pill streams the
    // rest in + switches to a faster grid layout (cose on 500 nodes is the
    // jank source). The full set stays accessible via the keyboard node
    // index (buildNodeIndex lists every node regardless of LOD), and
    // showDetail/applyPresenceFocus add a focused node on demand so the
    // spatial-presence halo still works on a hidden node (decluster-on-focus).
    var GRAPH_LOD_THRESHOLD = window.OKF_LOOM_GRAPH_LOD_THRESHOLD || 100;
      var totalNodeCount = bundle.nodes.length;
    var degreeOf = {};
    bundle.nodes.forEach(function (n) {
      var id = n.data.id;
      degreeOf[id] = (outgoing[id] || []).length + (backlinks[id] || []).length;
    });
    // Phase 3: top-decile hub threshold (degree rank). Top-decile = the
    // top ceil(N/10) nodes by degree; a single hub for a 6-node bundle,
    // ~10 hubs at the LOD cap. Degree-0 nodes can never be hubs (orphans
    // are not structurally central). The threshold is recomputed whenever
    // degrees change (init, LOD show-all, live refresh) so the halo set
    // always reflects the CURRENT graph topology, not a stale snapshot.
    var HUB_DEGREE_THRESHOLD = 0;
    function recomputeHubThreshold(nodeList) {
      var pos = nodeList.filter(function (n) {
        return (degreeOf[n.data.id] || 0) > 0;
      }).map(function (n) { return degreeOf[n.data.id] || 0; }).sort(function (a, b) { return a - b; });
      if (!pos.length) { HUB_DEGREE_THRESHOLD = Infinity; return; }
      // ceil(N/10) hubs ⇒ the cutoff is the degree at index N - ceil(N/10).
      var hubCount = Math.max(1, Math.ceil(pos.length / 10));
      var cutoffIdx = pos.length - hubCount;
      HUB_DEGREE_THRESHOLD = pos[cutoffIdx] || pos[0];
    }
    recomputeHubThreshold(bundle.nodes);
    // shownIds: the node ids currently in the Cytoscape canvas. Starts as the
    // full set when under threshold, or the top-N by degree when over.
      var shownIds = {};
      var lodActive = false;
      var lodShowAllExplicit = false;
      var lodPill = null;
      function recomputeLodState(candidate) {
        totalNodeCount = candidate.nodes.length;
        degreeOf = {};
        candidate.nodes.forEach(function (n) { var id=n.data.id; degreeOf[id]=(candidate.outgoing[id]||[]).length+(candidate.backlinks[id]||[]).length; });
        var nextShown = {};
        lodActive = totalNodeCount > GRAPH_LOD_THRESHOLD && !lodShowAllExplicit;
        if (lodActive) {
          candidate.nodes.slice().sort(function (a,b) { var da=degreeOf[a.data.id]||0,db=degreeOf[b.data.id]||0; return da!==db?db-da:String(a.data.id).localeCompare(String(b.data.id)); }).slice(0, GRAPH_LOD_THRESHOLD).forEach(function (n) { nextShown[n.data.id]=true; });
        } else candidate.nodes.forEach(function (n) { nextShown[n.data.id]=true; });
        shownIds = nextShown;
        // Phase 3: hub decile is topology-dependent; recompute on every
        // degree recomputation so the halo set tracks the live graph.
        recomputeHubThreshold(candidate.nodes);
      }
    if (totalNodeCount > GRAPH_LOD_THRESHOLD) {
      lodActive = true;
      // Sort a copy of nodes by (degree desc, id asc) for determinism, take N.
      var ranked = bundle.nodes.slice().sort(function (a, b) {
        var da = degreeOf[a.data.id] || 0, db = degreeOf[b.data.id] || 0;
        if (da !== db) return db - da;
        var la = String(a.data.id), lb = String(b.data.id);
        return la < lb ? -1 : (la > lb ? 1 : 0);
      });
      // Keep at least the threshold; never more than totalNodeCount.
      var keep = Math.min(GRAPH_LOD_THRESHOLD, totalNodeCount);
      for (var si = 0; si < keep; si++) shownIds[ranked[si].data.id] = true;
    } else {
      bundle.nodes.forEach(function (n) { shownIds[n.data.id] = true; });
    }

    // elemFor(n): builds the Cytoscape node datum from a bundle node.
    // `baseColor`/`size` are the intrinsic type colour + body-length size;
    // `color`/`vizSize` are the CURRENT display values the Signal-controls
    // "Colour by" and "Scale nodes by importance" options drive (so the
    // intrinsic values are always restorable). Keeping both means a colour/
    // size override never destroys the original mapping (state-ownership).
    // Phase 3: node glyphs. The server ships icon_paths (icon-key → SVG
    // inner markup, one source of truth with the wiki) + a per-node icon
    // key. Each key becomes an SVG data-URI stroked in the palette's
    // --graph-node-icon and drawn on the neutral round node. The cache is
    // keyed by glyph AND ink, so a palette change regenerates every glyph
    // (refreshNodeGlyphs) instead of reusing another palette's stroke.
    var ICON_PATHS = bundle.icon_paths || {};
    var _iconUriCache = {};
    function iconDataUri(key) {
      if (!key || !ICON_PATHS[key]) key = "file";
      if (!ICON_PATHS[key]) return null;
      var ink = graphInk().nodeIcon, cacheKey = key + "|" + ink;
      if (_iconUriCache[cacheKey]) return _iconUriCache[cacheKey];
      // Explicit width/height give the glyph an intrinsic size. Without it,
      // Chrome drew the image about twice too large on HiDPI canvases
      // (devicePixelRatio 2) and the node clipped it to its top-left corner.
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"'
        + ' stroke="' + ink + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + ICON_PATHS[key] + '</svg>';
      var uri = "data:image/svg+xml;utf8," + encodeURIComponent(svg);
      _iconUriCache[cacheKey] = uri;
      return uri;
    }
    // Ledger graph nodes are round records: the glyph carries the type, the
    // shape never does (DESIGN.md "Graph": neutral round nodes).
    function shapeForIcon() { return "ellipse"; }

    function elemFor(n) {
      // Content types are not colour-coded: every node starts on the neutral
      // --graph-node fill; a lens may recolour it from the muted ramp.
      var base = graphPalette().node;
      // Review feedback: smaller base node sizes. With the MAX_ZOOM clamp the
      // old 32..80 range rendered as giant discs on small bundles, colliding
      // with labels/edges (reviewer blocker 1). 22..44 reads cleanly.
      var sz = n.data.size || (22 + Math.min(22, ((bodies[n.data.id] || "").length / 320)));
      var iconKey = n.data.icon || "file";
      var hasComments = Object.prototype.hasOwnProperty.call(n.data, "active_comment_count");
      var commentCount = hasComments ? n.data.active_comment_count : null;
      return {
        data: Object.assign({}, n.data, {
          id: n.data.id,
          label: n.data.label || n.data.id,
          type: n.data.type,
          description: n.data.description || "",
          resource: n.data.resource || "",
          tags: n.data.tags || [],
          color: base,
          baseColor: base,
          size: sz,
          vizSize: sz,
          iconUrl: iconDataUri(iconKey),
          shape: shapeForIcon(iconKey),
          // Phase 3: expose the server-shipped structural fields on the
          // cy node so visual encoding (broken-link halo) and tests can
          // read them via cy.nodes().data(...) without consulting the
          // bundle lookup. Defaults to 0 when the server omitted them
          // (older single-file bundles pre-Phase-3) — fail-closed.
          degree: Number(n.data.degree) || 0,
           unresolved_count: Number(n.data.unresolved_count) || 0,
           active_comment_count: commentCount,
           has_active_comment_count: hasComments ? 1 : 0,
           // The canvas badge is a positive-count chip: a zero renders no
           // badge (same rule as the index-card comment chips). The count
           // itself stays on the node data, so detail signals, density
           // classes, and live refreshes keep the real value.
           commentBadgeUrl: hasComments && commentCount > 0 ? commentBadgeDataUri(commentCount) : "none",
           brokenBadgeUrl: (Number(n.data.unresolved_count) || 0) > 0 ? brokenBadgeDataUri() : "none",
         })
      };
    }
    function commentCountText(count) { return count > 99 ? "99+" : String(count); }
    function commentPhrase(count) { return count + " active comment" + (count === 1 ? "" : "s"); }
    function commentBadgeDataUri(count) {
      // A square outlined count chip with a mono figure (Ledger badge): band
      // fill, 1.2px --border-strong rule, ink figure. Its square silhouette
      // stays distinct from the broken-link triangle and the round node.
      // Exact counts remain in HTML; only this bounded canvas copy is capped.
      // (SVG images cannot reach page fonts, so the mono stack falls back to
      // the platform monospace inside the data URI.)
      var text = commentCountText(count);
      var width = 26;
      var badgePalette = graphInk();
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + width + '" height="18" viewBox="0 0 ' + width + ' 18">' +
        '<path d="M1 1H' + (width - 1) + 'V17H1Z" fill="' + badgePalette.chip + '" stroke="' + badgePalette.nodeBorder + '" stroke-width="1.2"/>' +
        '<text x="' + (width / 2) + '" y="12.6" text-anchor="middle" font-family="Azeret Mono,ui-monospace,monospace" font-size="9.5" font-weight="500" fill="' + badgePalette.nodeText + '">' + text + '</text></svg>';
      return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
    }
    function brokenBadgeDataUri() {
      // Persistent shape + glyph channel for the canvas warning: an outlined
      // triangle and "!" in the muted error hue (status hues mark strokes and
      // glyphs, never fills), on the halo so it reads over edges. The shape
      // keeps broken+comment+selected+hub states distinguishable.
      var badgePalette=graphInk();
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="20" viewBox="0 0 22 20">' +
        '<path d="M11 1.5 20.5 18.5H1.5Z" fill="'+badgePalette.halo+'" stroke="'+badgePalette.error+'" stroke-width="1.6" stroke-linejoin="miter"/>' +
        '<text x="11" y="15.5" text-anchor="middle" font-family="Azeret Mono,ui-monospace,monospace" font-size="10" font-weight="700" fill="'+badgePalette.error+'">!</text></svg>';
      return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
    }
    // edgeVisible(e): true only when BOTH endpoints are currently shown.
    function edgeVisible(e) {
      return shownIds[e.data.source] && shownIds[e.data.target];
    }
    function edgeElementData(e, weight) {
      var data=Object.assign({},e.data,{id:runtimeEdgeId(e.data),weight:weight});
      data.bundleLabel=data.multiplicity>1?String(data.multiplicity):"";
      return data;
    }
    // The relation type an edge is drawn as: its primary relation's canonical
    // type (an untyped legacy edge falls back to its label). Body links are
    // type "link" and draw as the plain solid edge; every other type is a
    // typed relation that carries a dash (and, under the Relations intent, a
    // muted hue). Anchor text never keys the encoding.
    function edgePrimaryType(data){var relations=Array.isArray(data.relations)?data.relations:[],primary=data.primary_relation||relations[Number(data.primary_relation_index)||0]||relations[0];return primary&&primary.type?String(primary.type):String(data.label||"");}
    function isTypedRelation(type){return !!type&&type!=="link";}
    function edgeRelations(edge){var relations=edge.data("relations");return Array.isArray(relations)&&relations.length?relations:[{id:edge.id()+"__relation",type:edge.data("label")||"link",label:edge.data("label")||"related",kind:edge.data("label")?"relation":"body_link",direction:"outgoing",source:edge.data("source"),target:edge.data("target")}];}
    // Relationship filters intentionally use one normalized dimension:
    // canonical relation `type`. Labels remain explanatory legend/detail text.
    function relationVisible(relation){return !controlState.hiddenRelations[relation.type];}
    function visibleEdgeRelations(edge){return edgeRelations(edge).filter(relationVisible);}
    function edgeRelationValues(edge){var values={};edgeRelations(edge).forEach(function(relation){values[relation.type]=true;values[relation.label]=true;});return Object.keys(values).sort();}
    function visibleEdgeRelationValues(edge){var values={};visibleEdgeRelations(edge).forEach(function(relation){values[relation.type]=true;values[relation.label]=true;});return Object.keys(values).sort();}
    function allRelationValues(){var values={};(bundle.edges||[]).forEach(function(edge){(edge.data.relations||[]).forEach(function(relation){if(relation.type)values[relation.type]=(values[relation.type]||0)+1;});});return Object.keys(values).sort();}
    function relationValueCount(value){var count=0;(bundle.edges||[]).forEach(function(edge){(edge.data.relations||[]).forEach(function(relation){if(relation.type===value)count++;});});return count;}
    function filterStateHasVisibleInstance(kind,state){
      if(kind!=="Relationship")return Object.keys(palette).some(function(value){return !state[value];});
      return (bundle.edges||[]).some(function(edge){return (edge.data.relations||[]).some(function(relation){return !state[relation.type];});});
    }
    function commitHiddenFilterState(kind,target,candidate){if(!filterStateHasVisibleInstance(kind,candidate)){setStatusNote("At least one "+kind.toLowerCase()+" must remain visible.");return false;}replaceRecord(target,candidate);return true;}

    // Cytoscape elements: shown nodes + deduped edges between shown nodes.
    var elems = [];
    var seenEdge = {};
    bundle.nodes.forEach(function (n) {
      if (!shownIds[n.data.id]) return;
      elems.push(elemFor(n));
    });
    bundle.edges.forEach(function (e) {
      if (!edgeVisible(e)) return;
      var key = e.data.id;
      if (seenEdge[key]) return;
      seenEdge[key] = 1;
      var edgeData = edgeElementData(e,0.5); edgeData.id=key;
      elems.push({ data: edgeData });
    });

    // ====================================================================
    // Signal controls — client-side metric engine
    // --------------------------------------------------------------------
    // Everything below is computed from fields ALREADY present in
    // graph.json (degree, backlinks, tags, entities, citations, typed
    // relations) — no backend contract change (contract-runtime-parity).
    // Scores normalise to 0..1 per graph with all-zero/empty-safe handling
    // so a uniform or empty bundle never divides by zero.
    // ====================================================================
    function arr(v) { return Array.isArray(v) ? v : []; }
    function citeKey(c) { return (c && (c.id || c.text)) || ""; }
    function setOf(list, keyFn) {
      var s = {};
      arr(list).forEach(function (x) {
        var k = keyFn ? keyFn(x) : x;
        if (k) s[String(k).toLowerCase()] = true;
      });
      return s;
    }
    function overlap(aSet, list, keyFn) {
      var n = 0;
      arr(list).forEach(function (x) {
        var k = keyFn ? keyFn(x) : x;
        if (k && aSet[String(k).toLowerCase()]) n++;
      });
      return n;
    }
    function entKey(e) { return e && (e.id || e.label); }

    // Per-node incident typed-relation count (resolved labelled edges) and
    // dominant incident edge label (for "Group by relation type").
    var incidentLabeled = {};
    var nodeRelLabel = {};
    var relationLabelSet = {};
    var relationLabelList = [];
    (function () {
      var counts = {};
      bundle.edges.forEach(function (e) {
        var primaryType = edgePrimaryType(e.data);
        if (primaryType && !relationLabelSet[primaryType]) { relationLabelSet[primaryType] = true; relationLabelList.push(primaryType); }
        (e.data.relations || []).forEach(function(relation){
          [e.data.source, e.data.target].forEach(function (nid) {
            incidentLabeled[nid] = (incidentLabeled[nid] || 0) + 1;
            counts[nid] = counts[nid] || {};
            counts[nid][relation.type] = (counts[nid][relation.type] || 0) + 1;
          });
        });
      });
      relationLabelList.sort();
      Object.keys(counts).forEach(function (nid) {
        var best = null, bestN = -1;
        Object.keys(counts[nid]).forEach(function (lab) {
          if (counts[nid][lab] > bestN) { bestN = counts[nid][lab]; best = lab; }
        });
        nodeRelLabel[nid] = best;
      });
    })();

    // Raw per-node scores.
    var raw = { links: {}, typed: {}, tags: {}, cites: {}, ents: {}, backl: {} };
    bundle.nodes.forEach(function (n) {
      var d = n.data, id = d.id;
      raw.links[id] = (outgoing[id] || []).length + (backlinks[id] || []).length;
      raw.typed[id] = arr(d.relations).length + (incidentLabeled[id] || 0);
      raw.cites[id] = arr(d.citations).length;
      raw.backl[id] = (backlinks[id] || []).length;
      var tagSet = setOf(d.tags);
      var entSet = setOf(d.entities, entKey);
      var nbrs = {};
      (outgoing[id] || []).forEach(function (t) { nbrs[t] = true; });
      (backlinks[id] || []).forEach(function (s) { nbrs[s] = true; });
      var tg = 0, en = 0;
      Object.keys(nbrs).forEach(function (nid) {
        var nd = nodeIndex[nid];
        if (!nd) return;
        tg += overlap(tagSet, nd.tags);
        en += overlap(entSet, nd.entities, entKey);
      });
      raw.tags[id] = tg;
      raw.ents[id] = en;
    });
    function maxOf(map) { var m = 0; Object.keys(map).forEach(function (k) { if (map[k] > m) m = map[k]; }); return m; }
    var rmax = {}, hasSignal = {};
    Object.keys(raw).forEach(function (k) { rmax[k] = maxOf(raw[k]); hasSignal[k] = rmax[k] > 0; });
    function normNode(metric, id) {
      var mx = rmax[metric];
      return mx ? (raw[metric][id] || 0) / mx : 0;
    }

    // Deduped edge list + per-signal raw/normalised edge scores.
    var edgeList = [], edgeSeenM = {}, edgeHasLabel = {};
    bundle.edges.forEach(function (e) {
      var s = e.data.source, t = e.data.target;
      if (!s || !t) return;
      var key = e.data.id;
      if (edgeSeenM[key]) { if (e.data.label) edgeHasLabel[key] = true; return; }
      edgeSeenM[key] = true;
      edgeList.push({ key: key, s: s, t: t });
      if (e.data.label) edgeHasLabel[key] = true;
    });
    var edgeRawMap = { links: {}, typed: {}, tags: {}, cites: {}, ents: {}, backl: {} };
    edgeList.forEach(function (e) {
      var s = e.s, t = e.t, k = e.key;
      var ds = nodeIndex[s] || {}, dt = nodeIndex[t] || {};
      edgeRawMap.links[k] = ((raw.links[s] || 0) + (raw.links[t] || 0)) / 2;
      edgeRawMap.typed[k] = edgeHasLabel[k] ? 1 : 0;
      edgeRawMap.tags[k] = overlap(setOf(ds.tags), dt.tags);
      edgeRawMap.ents[k] = overlap(setOf(ds.entities, entKey), dt.entities, entKey);
      edgeRawMap.cites[k] = overlap(setOf(ds.citations, citeKey), dt.citations, citeKey);
      edgeRawMap.backl[k] = (raw.backl[t] || 0);
    });
    var emax = {}, eHasSignal = {};
    Object.keys(edgeRawMap).forEach(function (k) { emax[k] = maxOf(edgeRawMap[k]); eHasSignal[k] = emax[k] > 0; });
    function normEdge(metric, key) {
      var mx = emax[metric];
      return mx ? (edgeRawMap[metric][key] || 0) / mx : 0;
    }

    function recomputeAcceptedMetrics(candidate) {
      incidentLabeled = {}; nodeRelLabel = {}; relationLabelSet = {}; relationLabelList = [];
      var counts = {};
      candidate.edges.forEach(function (e) {
        var d=e.data||{}, label=edgePrimaryType(d);
        if (!label) return;
        if (!relationLabelSet[label]) { relationLabelSet[label]=true; relationLabelList.push(label); }
        [d.source,d.target].forEach(function (id) {
          incidentLabeled[id]=(incidentLabeled[id]||0)+1;
          counts[id]=counts[id]||{}; counts[id][label]=(counts[id][label]||0)+1;
        });
      });
      relationLabelList.sort();
      Object.keys(counts).forEach(function (id) {
        var best=null,bestN=-1;
        Object.keys(counts[id]).sort().forEach(function (label) {
          if (counts[id][label]>bestN) { best=label; bestN=counts[id][label]; }
        });
        nodeRelLabel[id]=best;
      });
      raw={links:{},typed:{},tags:{},cites:{},ents:{},backl:{}};
      candidate.nodes.forEach(function (n) {
        var d=n.data,id=d.id,nbrs={};
        raw.links[id]=(outgoing[id]||[]).length+(backlinks[id]||[]).length;
        raw.typed[id]=arr(d.relations).length+(incidentLabeled[id]||0);
        raw.cites[id]=arr(d.citations).length; raw.backl[id]=(backlinks[id]||[]).length;
        (outgoing[id]||[]).concat(backlinks[id]||[]).forEach(function (other) { nbrs[other]=true; });
        var tags=0,ents=0,tagSet=setOf(d.tags),entSet=setOf(d.entities,entKey);
        Object.keys(nbrs).forEach(function (other) { var nd=nodeIndex[other]; if(!nd)return; tags+=overlap(tagSet,nd.tags); ents+=overlap(entSet,nd.entities,entKey); });
        raw.tags[id]=tags; raw.ents[id]=ents;
      });
      rmax={};hasSignal={};Object.keys(raw).forEach(function(k){rmax[k]=maxOf(raw[k]);hasSignal[k]=rmax[k]>0;});
      edgeList=[];edgeSeenM={};edgeHasLabel={};
      candidate.edges.forEach(function(e){var d=e.data||{},s=d.source,t=d.target,k=d.id;if(!s||!t||!k)return;if(edgeSeenM[k]){if(d.label)edgeHasLabel[k]=true;return;}edgeSeenM[k]=true;edgeList.push({key:k,s:s,t:t});if(d.label)edgeHasLabel[k]=true;});
      edgeRawMap={links:{},typed:{},tags:{},cites:{},ents:{},backl:{}};
      edgeList.forEach(function(e){var ds=nodeIndex[e.s]||{},dt=nodeIndex[e.t]||{},k=e.key;edgeRawMap.links[k]=((raw.links[e.s]||0)+(raw.links[e.t]||0))/2;edgeRawMap.typed[k]=edgeHasLabel[k]?1:0;edgeRawMap.tags[k]=overlap(setOf(ds.tags),dt.tags);edgeRawMap.ents[k]=overlap(setOf(ds.entities,entKey),dt.entities,entKey);edgeRawMap.cites[k]=overlap(setOf(ds.citations,citeKey),dt.citations,citeKey);edgeRawMap.backl[k]=raw.backl[e.t]||0;});
      emax={};eHasSignal={};Object.keys(edgeRawMap).forEach(function(k){emax[k]=maxOf(edgeRawMap[k]);eHasSignal[k]=emax[k]>0;});
    }

    // ---- Control state (single source of truth) -------------------------
    var SIGNAL_METRIC = { links: "links", typed: "typed", tags: "tags", cites: "cites", ents: "ents", backl: "backl" };
    var SIGNAL_LABELS = {
      mixed: "mixed signal", links: "link count", typed: "typed relations",
      tags: "shared tags", cites: "citations", ents: "entity overlap", backl: "backlinks"
    };
    var GROUP_LABELS = {
      community: "theme",
      none: "nothing", type: "type", tag: "tag", relation: "relation type",
      folder: "folder", neighborhood: "neighbourhood",
      graph_cluster: "graph cluster", source_system: "source system",
      project: "project", section: "section", import_batch: "import batch",
      redmine_project: "Redmine project"
    };
    var MIN_THRESH = [0, 0.25, 0.5, 0.72];
    var MIN_LABELS = ["Show all", "Weak and up", "Medium and up", "Strong only"];
    // Widened from 3 → 5 steps with a much higher ceiling so the user
    // can push nodes far apart / pull groups together hard (the 3-step range was
    // "too bunched up"). Index 1 (Balanced / Clear / Medium) stays the calm
    // default; presets sit in the low-mid band and the high end is reached
    // manually. Slider `max` is derived from these arrays' length (see rangeRow
    // calls) so index ↔ value ↔ label never drift out of lockstep.
    var SPACE_LABELS = ["Compact", "Balanced", "Open", "Wide", "Vast"];
    var SEP_LABELS = ["Soft", "Clear", "Wide", "Far", "Expanse"];
    var STR_LABELS = ["Low", "Medium", "High", "Strong", "Magnetic"];
    var SPACING = [0.55, 1.0, 1.7, 2.6, 4.0], SEPAR = [0.55, 1.0, 1.8, 2.8, 4.2], GROUPF = [0.3, 0.6, 0.9, 1.25, 1.6];
    var BRIDGE_CUT = 0.55;

    var controlState = {
      // Map is the default lens — community colors + PageRank
      // sizing on a force layout (the Gephi/InfraNodus composite; type is
      // carried by the node ICON, not color).
      preset: "map", lens: "map", layout: layoutName,
      spacing: 1, separation: 1, groupStrength: 1,
      groupBy: "community",
      colorMode: "community", sizeMode: "pagerank",
      // Compatibility signal-engine fields (edge weighting still uses them).
      colorBy: "type", primarySignal: "mixed", minLevel: 0,
      boostTyped: false, boostBacklinks: false,
      scaleByImportance: false, showBridges: false,
      focusEnabled: false, focusDepth: 1,
      // Review feedback: relation encoding lives on EDGES (colour + dash +
      // label), not node fills; edge labels are shown contextually to cut
      // clutter (reviewer blockers 6/8).
        relationEdges: false, showEdgeLabels: false,
        // Phase 4: legend-chip type filter (type name → true when hidden).
        hiddenTypes: {},
        hiddenRelations: {},
        search: "", type: "",
        // Only the explicit Advanced relationship-label control may reveal
        // the complete relationship set. This remains session-only.
        relationshipDetailOptIn: false
      };
    var currentLayoutName = layoutName;
    var focusRoot = null;
    // Sole owner for graph interaction state. Canvas classes and HTML controls
    // are projections; pointer, keyboard, URL and refresh all use dispatch().
    var interactionState = { selected: null, active: null, hovered: null,
      pathStart: null, pathEnd: null, pathResult: null, isolated: null,
      focusInvoker: null, priorLens: null, priorViewport: null, hoverGeneration: 0 };
    var interactionSnapshotRevision = 0;
    function graphInteractionSnapshot() {
      return Object.freeze({
        revision: interactionSnapshotRevision,
        active: interactionState.active,
        selected: interactionState.selected,
        hovered: interactionState.hovered,
        pathStart: interactionState.pathStart,
        pathEnd: interactionState.pathEnd,
        pathResult: interactionState.pathResult,
        isolated: interactionState.isolated,
        lens: controlState.lens,
        neighborhood: focusRoot ? Object.freeze({root:focusRoot,depth:controlState.focusDepth}) : null,
        path: interactionState.pathStart && interactionState.pathEnd ? Object.freeze({start:interactionState.pathStart,end:interactionState.pathEnd,result:interactionState.pathResult}) : null,
        filters: Object.freeze({search:controlState.search,type:controlState.type,hiddenTypes:Object.freeze(Object.assign({},controlState.hiddenTypes)),hiddenRelations:Object.freeze(Object.assign({},controlState.hiddenRelations))})
      });
    }
    function publishInteractionSnapshot(source) {
      interactionSnapshotRevision++;
      var snapshot=graphInteractionSnapshot();
      latestGraphInteractionSnapshot=snapshot;
      try { window.dispatchEvent(new CustomEvent("okf-loom:graph-interaction",{detail:{source:source||"controller",snapshot:snapshot}})); } catch(e) {}
      return snapshot;
    }
    var hoverTimer = null, hoverTransitionTimer = null;
    // ASYNC_LIFECYCLE_MATRIX (Phase 4 interaction boundary)
    // hover delay | owner: hoverGeneration | mutation: classes/tip after 120ms |
    // public: interaction snapshot | persisted: none | late: token-dropped |
    // retry: next pointer generation | proof: phase4_review_hover_lifecycle.
    // layout | owner: layoutSeq/deadline | mutation: positions/readiness |
    // public: capture state + retry label | persisted: none | late: seq-dropped |
    // retry: fresh runLayoutNow | proof: existing stale-layout + slice3 recovery.
    // fullscreen | owner: fullscreenGeneration/page lifecycle | mutation: browser
    // fullscreen/focus | public: button/status | persisted: none | late: pagehide-
    // fenced | retry: fresh click | proof: phase4_review_fullscreen_late_fence.
    // viewport restore | owner: current layout result | mutation: pan/zoom |
    // public: capture readiness | persisted: prior Focus snapshot | late: cleared
    // by URL/pagehide/new Focus | retry: new Focus cycle | proof: slice2 restore.
    // refresh deletion | owner: accepted graph request generation | mutation:
    // selected/active/focus/path/action/URL reconciliation | public: selection
    // event + capture revision/status | persisted: accepted revision only | late:
    // stale response dropped | retry: next SSE/manual refresh | proof:
    // group_d_graph_refresh_deletion + graph_refresh_latest_wins.
    // capture lease | owner: capture consumer deadline + lease generation |
    // mutation: dash offset frozen only immediately around screenshot | public:
    // token/release bool + screenshot result | persisted: no partial artifact |
    // late: stale release cannot resume newer lease; pagehide invalidates |
    // retry: fresh token | proof: capture consumer + edge-flow lease matrices.
    // Lens state — declared early: groupKeyFor('community') runs
    // during the FIRST applyGrouping pass, before the analytics block's
    // statements execute (var initializers do not hoist).
    var communityOf = {};
    var _lensCache = {};
    var panelEl = null;   // the Signal-controls <details>, for overlay-aware fit
    var GRAPH_EMPTY = totalNodeCount === 0;

    // Does the bundle expose a usable folder structure in its ids? Only then
    // do we offer "Group by folder" (advisor: do not advertise path grouping
    // unless it is reliably computable from ids).
    function folderOf(id) { var i = String(id).lastIndexOf("/"); return i > 0 ? id.slice(0, i) : "root"; }
    var HAS_FOLDERS = (function () {
      var seen = {}, n = 0;
      Object.keys(nodeIndex).forEach(function (id) { var f = folderOf(id); if (!seen[f]) { seen[f] = true; n++; } });
      return n > 1;
    })();
    var HAS_GRAPH_CLUSTER = hasMetaValue("graph_cluster");
    var HAS_SOURCE_SYSTEM = hasMetaValue("source_system");
    var HAS_PROJECT = hasMetaValue("project");
    var HAS_SECTION = hasMetaValue("section");
    var HAS_IMPORT_BATCH = hasMetaValue("import_batch");
    var HAS_REDMINE_PROJECT = hasMetaValue("redmine_project");

    // Connected components (for "Group by neighbourhood"); static per graph.
    var components = (function () {
      var comp = {}, cid = 0;
      Object.keys(nodeIndex).forEach(function (id) {
        if (comp[id] != null) return;
        var stack = [id]; comp[id] = cid;
        while (stack.length) {
          var cur = stack.pop();
          (outgoing[cur] || []).concat(backlinks[cur] || []).forEach(function (nb) {
            if (comp[nb] == null && nodeIndex[nb]) { comp[nb] = cid; stack.push(nb); }
          });
        }
        cid++;
      });
      return comp;
    })();

    // ---- Grouping ------------------------------------------------------
    var groupOf = {}, groupList = [], bridgeNorm = {};
    function groupKeyFor(id) {
      var d = nodeIndex[id] || {};
      switch (controlState.groupBy) {
        case "type": return d.type || "concept";
        case "tag": return arr(d.tags)[0] || "untagged";
        case "relation": return nodeRelLabel[id] || "untyped";
        case "folder": return folderOf(id);
        case "neighborhood": return "cluster " + (components[id] != null ? components[id] : 0);
        case "graph_cluster": return metaValue(d, "graph_cluster") || "unclustered";
        case "source_system": return metaValue(d, "source_system") || "unknown source";
        case "project": return metaValue(d, "project") || "unassigned project";
        case "section": return metaValue(d, "section") || "unassigned section";
        case "import_batch": return metaValue(d, "import_batch") || "unbatched";
        case "redmine_project": return metaValue(d, "redmine_project") || "unassigned Redmine project";
        // Detected communities drive the layout's
        // pull-together forces so themes read as spatial regions.
        case "community": return "theme " + (communityOf[id] != null ? communityOf[id] : 0);
        default: return "";
      }
    }
    function applyGrouping() {
      groupOf = {};
      var seen = {};
      Object.keys(nodeIndex).forEach(function (id) {
        var g = groupKeyFor(id); groupOf[id] = g; if (g) seen[g] = true;
      });
      groupList = Object.keys(seen).sort();
      // Bridge score: how many DISTINCT groups a node's neighbours span. A
      // node wiring together many groups is a structural bridge — a cheap,
      // dependency-free topological proxy (no betweenness plugin needed).
      var rawB = {}, mx = 0;
      Object.keys(nodeIndex).forEach(function (id) {
        var gs = {};
        (outgoing[id] || []).concat(backlinks[id] || []).forEach(function (nb) {
          var g = groupOf[nb]; if (g) gs[g] = true;
        });
        var c = Object.keys(gs).length;
        rawB[id] = c; if (c > mx) mx = c;
      });
      Object.keys(rawB).forEach(function (id) { bridgeNorm[id] = mx ? rawB[id] / mx : 0; });
    }

    // ---- Weighting (importance per chosen signal + boosts) -------------
    function applyBoost(base, typedN, backlN) {
      var num = base, denom = 1;
      if (controlState.boostTyped) { num += 0.5 * typedN; denom += 0.5; }
      if (controlState.boostBacklinks) { num += 0.5 * backlN; denom += 0.5; }
      var v = num / denom;
      return v < 0 ? 0 : (v > 1 ? 1 : v);
    }
    function nodeMixed(id) {
      var parts = ["links", "typed", "tags", "cites", "ents", "backl"], sum = 0, k = 0;
      parts.forEach(function (m) { if (hasSignal[m]) { sum += normNode(m, id); k++; } });
      return k ? sum / k : 0;
    }
    function nodeWeight(id) {
      var sig = controlState.primarySignal;
      var base = sig === "mixed" ? nodeMixed(id) : normNode(SIGNAL_METRIC[sig], id);
      return applyBoost(base, normNode("typed", id), normNode("backl", id));
    }
    function edgeMixed(key) {
      var parts = ["links", "typed", "tags", "cites", "ents", "backl"], sum = 0, k = 0;
      parts.forEach(function (m) { if (eHasSignal[m]) { sum += normEdge(m, key); k++; } });
      return k ? sum / k : 0;
    }
      function edgeWeight(key) {
        var sig = controlState.primarySignal;
        var base = sig === "mixed" ? edgeMixed(key) : normEdge(SIGNAL_METRIC[sig], key);
        return applyBoost(base, normEdge("typed", key), normEdge("backl", key));
      }

      // ---- P1 Progressive disclosure: deterministic high-signal backbone ----
      // The full edge set (e.g. 243 edges on the docs bundle) renders as an
      // unreadable hairball. The backbone keeps a deterministic subset that
      // preserves connectivity (maximum spanning forest) plus the strongest
      // 2–4 edges per node (top-K by weight). Same bundle → same subset on
      // every load (deterministic sort + stable union-find).
      var _backboneCache = {};
      function computeBackbone(extraPerNode) {
        extraPerNode = Math.max(0, Number(extraPerNode) || 0);
        if (_backboneCache[extraPerNode]) return _backboneCache[extraPerNode];
        // Rank all edges by current signal weight (deterministic tiebreak).
        var ranked = edgeList.map(function (e) {
          return { key: e.key, s: e.s, t: e.t, w: edgeWeight(e.key) };
        });
        ranked.sort(function (a, b) {
          if (b.w !== a.w) return b.w - a.w;
          return a.key < b.key ? -1 : (a.key > b.key ? 1 : 0);
        });
        // Maximum spanning forest via union-find (Kruskal) — preserves the
        // same connected components as the full graph.
        var parent = {};
        function find(x) {
          var orig = x;
          while (parent[x] !== undefined && parent[x] !== x) x = parent[x];
          if (parent[x] === undefined) parent[x] = x;
          // Path compression
          while (parent[orig] !== undefined && parent[orig] !== x) {
            var next = parent[orig]; parent[orig] = x; orig = next;
          }
          return parent[x];
        }
        var backbone = {};
        ranked.forEach(function (e) {
          var ra = find(e.s), rb = find(e.t);
          if (ra !== rb) { parent[ra] = rb; backbone[e.key] = true; }
        });
        // Optionally add strongest non-forest edges per node. Dense overview
        // uses zero extras and intermediate detail uses one.
        var perNode = {};
        ranked.forEach(function (e) {
          if (backbone[e.key]) return;
          if (!perNode[e.s]) perNode[e.s] = [];
          if (!perNode[e.t]) perNode[e.t] = [];
          perNode[e.s].push(e);
          perNode[e.t].push(e);
        });
        Object.keys(perNode).forEach(function (id) {
          perNode[id].slice(0, extraPerNode).forEach(function (e) { backbone[e.key] = true; });
        });
        _backboneCache[extraPerNode] = backbone;
        return backbone;
      }

    // ---- Colour ramps --------------------------------------------------
    function hashHue(str) {
      var h = 0, s = String(str);
      for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
      return h;
    }
    // Lens colours come from the palette-derived muted ramp (graphPalette()
    // .ramp); there is no type palette on the canvas. A group without a name
    // stays on the neutral node fill.
    function neutralNodeColor() { return graphPalette().node; }
    function groupColor(g) {
      if (!g) return neutralNodeColor();
      // Explicit "Colour by selected group": categorical muted hues. The
      // index follows the sorted group list so the first eight groups never
      // share a hue; names outside the list fall back to a stable hash.
      var cat = graphPalette().ramp.categorical, idx = groupList.indexOf(g);
      return cat[(idx >= 0 ? idx : hashHue(g)) % cat.length];
    }
    // Relation-type edge colour. Typed edges always carry their dash pattern
    // (the non-colour channel); hue is a lens encoding reserved for the
    // explicit Relations/Flow intent (strong=true). Otherwise every edge is
    // the neutral --graph-edge.
    function relColor(label, strong) {
      var pal = graphPalette();
      if (!strong) return pal.edge;
      return pal.ramp.relation[hashHue(label) % pal.ramp.relation.length];
    }
    function relDashClass(label) { return "okf-reld-" + (hashHue(label) % 3 + 1); }
    // Forced-colours lens cues: the lens encoding without hue. A category
    // index picks one of eight node shapes (the same index the categorical
    // ramp uses), an ordered value one of five stroke weights; undated
    // concepts are dotted. The canvas layer and the legend/index keys read
    // the same cue, so they always agree.
    function lensShapeCue(i) { return "shape-" + ((((Number(i) || 0) % 8) + 8) % 8); }
    function lensLevelCue(t) { return "level-" + Math.round(Math.max(0, Math.min(1, Number(t) || 0)) * 4); }
    function recencyCue(days) { return isFinite(days) ? lensLevelCue(1 - Math.min(1, days / 180)) : "undated"; }
    function groupCue(g) {
      if (!g) return "";
      var idx = groupList.indexOf(g);
      return lensShapeCue(idx >= 0 ? idx : hashHue(g));
    }
    function lensKeyTitle(cue, name) {
      return (FORCED_COLORS ? (cue === "shape" ? "Shape: " : "Outline: ") : "Colour: ") + name;
    }
    function setForcedCue(el, cue) {
      if (cue) el.setAttribute("data-forced-cue", cue);
      else el.removeAttribute("data-forced-cue");
    }
    function importanceColor(w) {
      // Neutral (low) → muted accent (high): one ordered ramp, not a
      // categorical palette (a11y: ordered scale).
      return graphPalette().ramp.sequence(w);
    }

    // ---- Layout tuning derived from the spacing/separation/strength knobs
    function idealEdgeFn(spaceMul, sepMul, gf) {
      return function (edge) {
        var base = 140 * spaceMul;
        if (controlState.groupBy === "none") return base;
        var same = groupOf[edge.data("source")] === groupOf[edge.data("target")];
        // Review feedback: FLOOR same-group edge length so high Group strength
        // can never collapse members into an overlapping pile (blocker 1). The
        // floor scales with node size (~3× node diameter), so even at the widened
        // Magnetic group strength members stay separated (resolveOverlaps is the
        // hard backstop regardless).
        // Stronger inter-group coefficient (1.0 → 1.2) so high Group
        // strength + Cluster separation push distinct groups FAR apart while
        // members stay tightly clustered — the "into groups more" contrast.
        return same ? Math.max(95 * spaceMul, base * (1 - 0.30 * gf))
                    : base * (1 + 1.2 * gf * sepMul);
      };
    }
    function layoutTuning(state) {
      var sp = SPACING[state.spacing], se = SEPAR[state.separation], gf = GROUPF[state.groupStrength];
      // Review feedback: small/dense bundles (e.g. the 6-node demo, near-
      // complete) collapse into an overlapping pile under plain cose. Boost
      // the effective spacing + repulsion and cut gravity hard so the few
      // nodes push apart into a readable spread; tapers to 1× by ~16 nodes so
      // large bundles are unaffected.
      var nN = (typeof cy !== "undefined" && cy && cy.nodes) ? cy.nodes().length : 30;
      var smallBoost = nN <= 14 ? Math.min(3, 16 / Math.max(nN, 4)) : 1;
      var spE = sp * smallBoost;
      return {
        // For tiny/dense graphs, amplify repulsion AND loosen edge springs so
        // a near-complete cluster opens up instead of collapsing to a ball.
        nodeRepulsion: 18000 * (0.7 + 0.6 * spE) * smallBoost,
        // Strong overlap repulsion, scaled up for small/dense graphs so the
        // few nodes never sit on top of each other.
        nodeOverlap: 24 * smallBoost,
        idealEdgeLength: idealEdgeFn(spE, se, gf),
        edgeElasticity: smallBoost > 1 ? 0.12 : 0.45,
        numIter: smallBoost > 1 ? 5000 : 3500,
        componentSpacing: 120 * spE * se,
        gravity: (0.16 / se) / smallBoost,
        spacingFactor: 1.0 + 0.5 * spE + 0.3 * (se - 1),
        minNodeSpacing: 30 + 40 * spE,
        radius: 200 + 150 * spE,
        sort: (state.groupBy === "none") ? undefined : function (a, b) {
          var ga = groupOf[a.id()] || "", gb = groupOf[b.id()] || "";
          if (ga < gb) return -1;
          if (ga > gb) return 1;
          var la = String(a.data("label") || a.id()), lb = String(b.data("label") || b.id());
          return la < lb ? -1 : (la > lb ? 1 : 0);
        }
      };
    }

      // Label precedence (D2-006). The selected, hovered or toured concept's
      // name is the one the reader is looking at: its node joins the top
      // layer (above its own incident edges and every neighbour), its label
      // sits on an opaque --surface plate with a hairline --border-strong rule
      // and shows more of the title. Every other label yields to it through
      // cullOverlappingLabels(), which hides (never moves) the lower-priority
      // label or count chip in an overlap. Culled text keeps its geometry, so
      // fitting and hit-testing do not change when a label is culled.
      var LABEL_PRECEDENCE = "node:selected, node.okf-hover, node.okf-tour-target";
      var LABEL_CULLED = "node.okf-label-culled, edge.okf-label-culled";
      var LABEL_CULLED_STYLE = { "text-opacity": 0, "text-background-opacity": 0, "text-outline-opacity": 0, "text-border-opacity": 0 };
      var PRECEDENCE_LABEL_WIDTH = 220;
      function labelPlateStyle(pal) {
        return {
          "color": pal.nodeText, "text-background-color": pal.surface, "text-background-opacity": 1,
          "text-background-padding": 3, "text-border-width": 1, "text-border-color": pal.nodeBorder,
          "text-border-opacity": 1, "text-max-width": PRECEDENCE_LABEL_WIDTH,
          "z-compound-depth": "top", "z-index": 40
        };
      }
      // Cytoscape's cascade is last-rule-wins, so every pass that appends a
      // base "node" label rule re-appends these after it.
      function labelPrecedenceRules(style, pal) {
        return style.selector(LABEL_PRECEDENCE).style(labelPlateStyle(pal)).selector(LABEL_CULLED).style(LABEL_CULLED_STYLE);
      }

      // Compute the initial (default-preset) grouping so the first layout is
      // already grouped-by-type rather than a flat hairball.
      applyGrouping();
      // The initial Cytoscape stylesheet must use the resolved theme too. The
      // later synchronizer remains necessary for theme events, but a dark
      // first paint must never briefly seed stateful cues from a light palette.
      var INITIAL_GRAPH_PALETTE = graphPalette();

      var container = document.getElementById("okf-graph");
    var cy = window.cytoscape({
      container: container,
      elements: elems,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            // Phase 3 node glyphs: type icon (an SVG data-URI stroked in
            // --graph-node-icon) centred on the neutral round node.
            "shape": "data(shape)",
            // Resolve image values in JS. Cytoscape treats data(...) inside a
            // multi-background array as a literal URL in some 3.x builds,
            // causing requests for /data(iconUrl) and /data(commentBadgeUrl).
            "background-image": function (n) {
              return [n.data("iconUrl") || "none", n.data("commentBadgeUrl") || "none", n.data("brokenBadgeUrl") || "none"];
            },
            "background-width": ["55%", "26px", "22px"],
            "background-height": ["55%", "18px", "20px"],
            "background-position-x": ["50%", "100%", "0%"],
            "background-position-y": ["50%", "0%", "0%"],
            "background-clip": ["node", "none", "none"],
            "background-image-containment": ["inside", "over", "over"],
            "bounds-expansion": 18,
            "label": "data(label)",
              "color": INITIAL_GRAPH_PALETTE.nodeText,
              "font-family": INITIAL_GRAPH_PALETTE.font,
            // Review feedback: node names read "near-microscopic" at the
            // widened max spread (the auto-fit zooms the big graph out, so the
            // rendered size is font-size × zoom; measured max-range zoom is
            // ~0.47–0.71). A bump (11→13) plus a 600 weight keeps the identity
            // labels legible at the extreme end, and the min-zoomed floor is
            // lowered 7→6 so node IDENTITIES stay on-screen across that whole
            // zoom band (13 × 0.47 ≈ 6 ≥ 6) instead of vanishing in the wider
            // layouts — the reviewer's "users cannot identify nodes". Nodes keep
            // a lower floor than the secondary edge labels (which hide at 9), so
            // identity survives zoom-out while edge-label noise is dropped first.
            "font-size": 13,
            "font-weight": 600,
            "min-zoomed-font-size": 6,
            "text-valign": "bottom",
            "text-margin-y": 4,
            // Phase 3: ellipsized labels at a fixed 90px ceiling so long
            // concept names cannot overrun their neighbours or pile onto
            // the canvas. The full name still appears on hover/selection
            // (showDetail re-renders the node's tooltip + panel with the
            // untruncated label), and the keyboard node index lists every
            // name in full. text-wrap:"ellipsis" is Cytoscape's native
            // single-line ellipsis mode (no JS truncation needed); the
            // prior "wrap" mode let long names stack into tall plates that
            // drove the label-collision pile-up.
            "text-wrap": "ellipsis",
            "text-max-width": 90,
            // Ledger labels: --graph-label ink on a --graph-halo outline, so
            // names stay legible over edges and neighbours without a plate.
            // The (square) plate colour is still set so selected/hovered
            // labels can raise a full halo plate (syncLabelColour).
              "text-outline-color": INITIAL_GRAPH_PALETTE.halo,
            "text-outline-width": 2,
            "text-outline-opacity": 1,
              "text-background-color": INITIAL_GRAPH_PALETTE.edgeLabelBg,
            "text-background-opacity": 0,
            "text-background-padding": 2,
            "text-background-shape": "rectangle",
            "width": "data(vizSize)",
            "height": "data(vizSize)",
            // Neutral round record: 1.5px --border-strong stroke.
            "border-width": 1.5,
              "border-color": INITIAL_GRAPH_PALETTE.nodeBorder,
            // Cytoscape underlays are round-rectangles; a zero corner radius
            // makes every halo (hub, hover, search, path, bridge) square.
            "underlay-shape": "round-rectangle",
            "underlay-corner-radius": 0,
          },
        },
        {
          // The accent's one canvas use: the selected node's 4px stroke.
          selector: "node:selected",
            style: { "border-width": 4, "border-color": INITIAL_GRAPH_PALETTE.select },
        },
        {
          selector: "edge",
          style: {
            // Width + opacity track the per-edge `weight` (0..1) the
            // Signal "Primary signal" + boost controls compute. mapData keeps
            // the mapping in the stylesheet so .dim / :selected class selectors
            // still override (an inline ele.style() bypass would not).
              "width": "mapData(weight, 0, 1, 1.2, 4.8)",
              // P1 edge noise: lowered the opacity floor (0.5→0.35) so the
              // default graph reads calmer. Incident edges (touching the
              // selected node) get the .okf-incident class which restores full
              // opacity — a localized, safe highlight on the existing selection
              // seam (showDetail → applyEdgeLabels).
              "opacity": "mapData(weight, 0, 1, 0.9, 1)",
              "line-color": INITIAL_GRAPH_PALETTE.edge,
              "target-arrow-color": INITIAL_GRAPH_PALETTE.edge,
            "target-arrow-shape": "triangle",
            // Stop terminal geometry before corner badges so arrowheads stay
            // visible on broken+comment compound-state nodes.
            "target-distance-from-node": 12,
            "source-distance-from-node": 5,
            "curve-style": "bezier",
            // Phase 3: arrowheads scale with edge weight so direction stays
            // readable on heavier edges (0.85 flat was squint-territory).
            "arrow-scale": "mapData(weight, 0, 1, 0.9, 1.6)",
            // Review feedback: edge labels are SUPPRESSED by default (clutter).
            // The .okf-show-label class turns them on contextually (Relations /
            // Focus presets, and edges touching the selected node).
            "label": "",
            // Review feedback: the contextual relationship labels were
            // faint/thin/unreadable at high spread. The faintness was the COLOR
              // and WEIGHT, so fix exactly those — a 600 weight and the
              // darkened slate token (the light-theme value is #334155 ≈ 10:1)
              // — and
            // leave the plate opacity/padding at their reviewed defaults so the
            // many overlapping selected-node labels do not stack into an opaque
            // blob at the compact default. min-zoomed-font-size is raised 7→9 so
            // that once the auto-fit zooms the graph out far enough that the
            // labels would be sub-legible, they LOD-hide entirely (clean canvas)
            // instead of rendering as faint noise; they return on zoom-in. This
            // preserves Relations/Focus semantics (labels are still emitted via
            // .okf-show-label) and only changes WHEN they paint.
            // D2-010: relationship labels are secondary text, so they read
            // in the muted --text-3 ink at a regular weight (still AA on the
            // 0.9 halo plate in every palette).
            "font-size": 11,
            "font-weight": 500,
              "color": INITIAL_GRAPH_PALETTE.edgeLabel,
              "font-family": INITIAL_GRAPH_PALETTE.font,
            "text-rotation": "autorotate",
              "text-background-color": INITIAL_GRAPH_PALETTE.edgeLabelBg,
            "text-background-opacity": 0.9,
            "text-background-padding": 2,
            "text-background-shape": "rectangle",
            "min-zoomed-font-size": 9,
          },
        },
          // Contextual edge labels (Relations/Focus + selected-node edges).
          { selector: "edge.okf-show-label, edge.okf-density-detail-label", style: { "label": "data(label)" } },
          // A compact count chip is always visible for bundled parallel
          // relationships; selecting it opens the lossless semantic list.
          // Ledger chip: square, band fill, 1px --border-strong rule, mono
          // figure in ink, never rotated.
          { selector: "edge.okf-multi-edge", style: {
            "label": "data(bundleLabel)", "font-size": 10, "font-weight": 500,
              "font-family": INITIAL_GRAPH_PALETTE.mono, "color": INITIAL_GRAPH_PALETTE.nodeText,
            "text-rotation": "none",
              "text-background-color": INITIAL_GRAPH_PALETTE.chip,
            "text-background-opacity": 1, "text-background-padding": 3,
            "text-background-shape": "rectangle", "text-border-width": 1,
              "text-border-color": INITIAL_GRAPH_PALETTE.nodeBorder,
            "text-border-opacity": 1
          } },
          { selector: "node.okf-density-overview-zero", style: {
              "background-image": function (n) { return [n.data("iconUrl") || "none", "none", "none"]; },
          } },
          { selector: "node.okf-density-overview-broken", style: {
              "background-image": function (n) { return [n.data("iconUrl") || "none", "none", n.data("brokenBadgeUrl") || "none"]; },
          } },
          { selector: "node.okf-broken-aggregated", style: {
              "background-image": function (n) { return [n.data("iconUrl") || "none", n.data("commentBadgeUrl") || "none", "none"]; },
          } },
          { selector: "node.okf-broken-aggregated:selected", style: {
              "background-image": function (n) { return [n.data("iconUrl") || "none", n.data("commentBadgeUrl") || "none", n.data("brokenBadgeUrl") || "none"]; },
          } },
          // P1 edge highlight: edges incident to the selected node pop above
          // the de-emphasized default edge noise. Restores full opacity + a
          // modest width bump. A raised z-index keeps incident edges above
          // non-incident ones so they are never visually buried, while edges
          // still pass BEHIND every node and label (a top-layer fan of
          // incident edges used to strike through the names it connects).
          // The colour is left to the base edge / relation encoding — this
          // is a PROMINENCE cue, not a new colour variable.
          { selector: "edge.okf-incident", style: {
              "opacity": 1,
              "width": 3,
              "z-index": 10,
          } },
          // P1 progressive disclosure: non-backbone edges are display:none so
          // they are excluded from BOTH rendering AND layout (cleaner initial
          // layout). aStar still traverses them (they remain in cy.elements()),
          // so path tracing finds paths through hidden edges; applyEdgeElision
          // then reveals path edges before the .okf-path highlight paints.
          { selector: "edge.okf-edge-elided", style: { "display": "none" } },
        // Relation-type encoding lives on the edge: colour + dash + (label).
        { selector: "edge.okf-rel-edge", style: { "line-color": "data(relColor)", "target-arrow-color": "data(relColor)" } },
        { selector: "edge.okf-reld-1", style: { "line-style": "dashed", "line-dash-pattern": [6, 3] } },
        { selector: "edge.okf-reld-2", style: { "line-style": "dashed", "line-dash-pattern": [2, 3] } },
        { selector: "edge.okf-reld-3", style: { "line-style": "dashed", "line-dash-pattern": [10, 3, 2, 3] } },
        // Phase 3: degree-zero concepts read as "not yet linked" — a dashed
        // stroke in the muted warning hue (status hues mark strokes, never
        // fills); fcose tiles them into a tray.
        { selector: "node.okf-orphan", style: {
            "border-style": "dashed",
            "border-width": 2,
              "border-color": INITIAL_GRAPH_PALETTE.warn,
            "opacity": 0.85,
        } },
        // Phase 3: top-decile hub halo. A square --surface-2 underlay marks
        // the small set of structurally central nodes (neutral, never the
        // accent); non-colour-only cue (size/icon still carry identity), and
        // it composes with .okf-bridge / .okf-focus-root / .okf-presence-halo
        // (those set their own underlay/border and win on z-index). Computed
        // deterministically by degree rank (see applyVisualEncoding). The
        // 3px padding keeps the square above the label, which starts 4px
        // below the disc (a 7px halo cut through the first letters).
        { selector: "node.okf-hub", style: {
                "underlay-color": INITIAL_GRAPH_PALETTE.raised,
            "underlay-opacity": 1,
            "underlay-padding": 3,
            "underlay-corner-radius": 0,
        } },
        // Phase 3: broken-link indicator. Nodes whose outgoing links
        // include unresolved targets (broken/missing per SPEC §5.3, as
        // counted by ``Graph.unresolved`` server-side) get a tight red
        // underlay ring. The count is also exposed textually in the hover
        // tooltip, the detail-panel "Why this connects" list, and the
        // keyboard node index ("!" prefix), so the cue is never colour-
        // only (a11y: non-colour channels exist for users who cannot
        // perceive the red ring or who are in forced-colours mode).
        { selector: "node.okf-broken-link", style: {
            "underlay-opacity": 0,
        } },
        { selector: "node.okf-density-label-hidden", style: { "label": "" } },
        { selector: "node.okf-density-label-hidden:selected, node.okf-density-label-hidden.okf-hover, node.okf-density-label-hidden.okf-search-match, node.okf-density-label-hidden.okf-focus-root, node.okf-density-label-hidden.okf-path", style: { "label": "data(label)" } },
        { selector: "edge.okf-unrelated", style: { "opacity": 0.12 } },
        // Phase 4 hover states: the pointed node's neighborhood stays at
        // full opacity while everything else fades; the node itself gets a
        // soft halo. Separate class from `.dim` so filters and hover
        // compose instead of fighting.
        { selector: ".okf-hover-dim", style: { "opacity": 0.18 } },
          { selector: "node.okf-search-match", style: { "underlay-color": INITIAL_GRAPH_PALETTE.raised, "underlay-opacity": 1, "underlay-padding": 7, "border-width": 3, "border-color": INITIAL_GRAPH_PALETTE.cue } },
        { selector: "node.okf-search-nonmatch", style: { "opacity": 0.25 } },
        { selector: "node.okf-hover", style: {
            "width": function (ele) { return (Number(ele.data("vizSize")) || 32) * 1.15; },
            "height": function (ele) { return (Number(ele.data("vizSize")) || 32) * 1.15; },
            "transition-property": REDUCED_MOTION ? "none" : "width height underlay-opacity",
            "transition-duration": REDUCED_MOTION ? "0ms" : "120ms",
              "underlay-color": INITIAL_GRAPH_PALETTE.raised,
            "underlay-opacity": 1,
            "underlay-padding": 8,
        } },
        // Phase 4 path tracing (shift-click): the chain is drawn in ink
        // (--text), heavier and solid; everything off-path fades further
        // than hover. The accent stays reserved for the selected node.
        { selector: ".okf-path-dim", style: { "opacity": 0.12 } },
        { selector: "node.okf-path", style: {
            "border-width": 3,
              "border-color": INITIAL_GRAPH_PALETTE.cue,
              "underlay-color": INITIAL_GRAPH_PALETTE.raised,
            "underlay-opacity": 1,
            "underlay-padding": 6,
        } },
        // The hovered directional edge dashes so its flow reads (path and
        // selected edges below keep their solid stroke).
        { selector: "edge.okf-edge-flow", style: { "line-style": "dashed" } },
        { selector: "edge.okf-path", style: {
              "line-color": INITIAL_GRAPH_PALETTE.cue,
              "target-arrow-color": INITIAL_GRAPH_PALETTE.cue,
            "width": 4.5,
            "opacity": 1,
            "line-style": "solid",
            "z-compound-depth": "top",
        } },
        {
          selector: "edge:selected",
          style: {
              "line-color": INITIAL_GRAPH_PALETTE.select,
              "target-arrow-color": INITIAL_GRAPH_PALETTE.select,
            "line-style": "solid",
            "width": 5,
            "opacity": 1,
          },
        },
        // Review feedback: bridge cue is now a CALM soft glow (underlay), not a
        // heavy double-black ring (reviewer blocker 1). Still a non-colour-only
        // SHAPE/halo cue; cross-group bridge edges stay dashed.
        { selector: "node.okf-bridge", style: {
              "underlay-color": INITIAL_GRAPH_PALETTE.raised,
            "underlay-opacity": 1,
            "underlay-padding": 6,
            "border-width": 2,
              "border-color": INITIAL_GRAPH_PALETTE.bridgeBorder,
        } },
        { selector: "edge.okf-bridge-edge", style: { "line-style": "dashed" } },
        // Focus root: the focused (selected) node keeps the accent stroke over
        // a wide neutral square halo so it is unmistakable.
        { selector: "node.okf-focus-root", style: {
              "underlay-color": INITIAL_GRAPH_PALETTE.raised,
            "underlay-opacity": 1,
            "underlay-padding": 14,
            "border-width": 4,
              "border-color": INITIAL_GRAPH_PALETTE.select,
            "z-index": 30,
        } },
        // Non-neighbour dim during focus is stronger (~12%); reviewer asked ≤30%.
        { selector: ".dim", style: { opacity: 0.12 } },
        // A node's opacity does not reach its underlay, so the structural
        // hub and bridge halos fade with their faded node explicitly instead
        // of leaving solid squares on a ghosted canvas (D2-014). The
        // strongest fade is last, so it wins when fades combine.
        { selector: "node.okf-hub.okf-search-nonmatch, node.okf-bridge.okf-search-nonmatch", style: { "underlay-opacity": 0.25 } },
        { selector: "node.okf-hub.okf-hover-dim, node.okf-bridge.okf-hover-dim", style: { "underlay-opacity": 0.18 } },
        { selector: "node.okf-hub.dim, node.okf-hub.okf-path-dim, node.okf-bridge.dim, node.okf-bridge.okf-path-dim", style: { "underlay-opacity": 0.12 } },
        // iter1 CRI-004: presence halo. When the agent's presence.focus
        // points at a concept, graph.js adds this class to that node so the
        // agent is spatially visible on the canvas, not just in the status
        // chip. The border uses the muted info hue (a status stroke) so the
        // agent's focus never reads as the reader's own selection.
        { selector: ".okf-presence-halo", style: {
            "border-width": 4,
              "border-color": INITIAL_GRAPH_PALETTE.info,
            "border-opacity": 1,
            "z-index": 20,
          } },
        // The selected node's 4px accent stroke wins over every state cue
        // above (path, search, bridge, orphan, presence).
        { selector: "node:selected", style: { "border-width": 4, "border-style": "solid", "border-color": INITIAL_GRAPH_PALETTE.select } },
        // Label precedence and culling (labelPrecedenceRules owns both).
        { selector: LABEL_PRECEDENCE, style: labelPlateStyle(INITIAL_GRAPH_PALETTE) },
        { selector: LABEL_CULLED, style: LABEL_CULLED_STYLE },
      ],
      // The deterministic circle/sparse/mobile seed below replaces ALL node
      // positions before runLayoutNow(). Do not compute a discarded grid (and
      // its label-aware bounding boxes) during element creation. The tuned
      // layout still runs once after the chrome exists, through its sole owner.
      layout: { name: "preset", animate: false, fit: false },
    });
    // Canvas metrics/label textures are keyed by the font-family string, not
    // by FontFace load state. A style().update() with the same string therefore
    // keeps fallback measurements AND textures. Re-key through the supported
    // stylesheet API once after late Ledger faces settle: a repeated generic
    // fallback is typographically identical, but invalidates both cache layers.
    // Never await fonts on the first-frame/layout path or rerun the layout.
    if (document.fonts) {
      var lateSans = INITIAL_GRAPH_PALETTE.font.indexOf("Public Sans") >= 0 && !document.fonts.check('600 13px "Public Sans"');
      var lateMono = INITIAL_GRAPH_PALETTE.mono.indexOf("Azeret Mono") >= 0 && !document.fonts.check('500 10px "Azeret Mono"');
      var fontMountGeneration = graphMountGeneration;
      if (lateSans || lateMono) document.fonts.ready.then(function () {
        if (fontMountGeneration !== graphMountGeneration || cy.destroyed()) return;
        var sansLoaded = lateSans && document.fonts.check('600 13px "Public Sans"');
        var monoLoaded = lateMono && document.fonts.check('500 10px "Azeret Mono"');
        if (!sansLoaded && !monoLoaded) return;
        var pal = graphPalette(), style = cy.style();
        if (sansLoaded) style.selector("node, edge").style("font-family", pal.font + ", sans-serif");
        // This later selector preserves the distinct bundled-edge mono face
        // even when the new base edge rule was appended after its original rule.
        style.selector("edge.okf-multi-edge").style("font-family", pal.mono + (monoLoaded ? ", monospace" : ""));
        style.update();
        cy.forceRender();
      });
    }
    // Review feedback: clamp zoom. cy.fit on a small/collapsed cluster used to
    // over-zoom (font-size scales with zoom → giant labels). maxZoom caps both
    // user and programmatic zoom; overlayAwareFit() also respects it.
    var MAX_ZOOM = 1.6;
    try { cy.maxZoom(MAX_ZOOM); cy.minZoom(0.06); } catch (e) {}
    var nativeGetElementById=cy.getElementById.bind(cy);
    cy.getElementById=function(id){var found=nativeGetElementById(id);if(found&&found.length)return found;if(typeof id==="string"&&id.indexOf("__")>=0){var endpointMatch=cy.edges().filter(function(edge){return edge.data("source")+"__"+edge.data("target")===id;});if(endpointMatch.length===1)return endpointMatch;}return found;};
      // Stateful graph cues are not all covered by the base node/edge rules:
      // Cytoscape resolves class selectors after those base rules. Keep their
      // palette-dependent colours in one consumer-side synchronizer so a
      // palette event repaints the active path, hover/search/hub/bridge
      // halos, focus, presence, orphans and bundled-edge count chips as one
      // transaction. Halos are the neutral raised surface; path, search and
      // bridge marks are ink; the accent stays on the selection.
      function syncStatefulGraphColours(pal) {
        cy.style()
          .selector("node.okf-search-match, node.okf-hover, node.okf-hub, node.okf-path, node.okf-focus-root, node.okf-bridge, node.okf-tour-target")
          .style("underlay-color", pal.raised)
          // Widths ride along: a base "node" rule appended by an earlier pass
          // (forced colours) must never thin these cues back to 1.5px.
          .selector("node.okf-orphan")
          .style({ "border-color": pal.warn, "border-width": 2, "border-style": "dashed" })
          .selector("node.okf-bridge")
          .style({ "border-color": pal.cue, "border-width": 2 })
          .selector("node.okf-search-match, node.okf-path")
          .style({ "border-color": pal.cue, "border-width": 3, "border-style": "solid" })
          .selector(".okf-presence-halo")
          .style({ "border-color": pal.info, "border-width": 4 })
          .selector("node.okf-focus-root")
          .style({ "border-color": pal.select, "border-width": 4, "border-style": "solid" })
          .selector("edge.okf-path")
          .style({ "line-color": pal.cue, "target-arrow-color": pal.cue })
          .selector("edge:selected")
          .style({ "line-color": pal.select, "target-arrow-color": pal.select })
          .selector("edge.okf-multi-edge")
          .style({ "text-border-color": pal.nodeBorder, "text-background-color": pal.chip, "color": pal.nodeText })
          .selector("node:selected")
          .style({ "border-color": pal.select, "border-width": 4, "border-style": "solid" })
          .update();
      }

    // Palette synchronisation. Every colour the canvas paints comes from
    // graphPalette() (the active palette's resolved tokens), so a
    // palette change re-applies labels, halos, strokes, edges, badges,
    // glyphs and the minimap here; repaintLensColours() recolours the lens
    // encoding itself (see syncGraphTheme).
    function syncBadgeDataUris() {
      // Badge SVG data URLs capture resolved theme values. Rebuild every
      // theme-dependent layer even while forced colours own Cytoscape's
      // runtime paint; theme.js remains the sole data-theme writer.
      cy.nodes().forEach(function (node) {
        if (node.data("has_active_comment_count") === 1) {
          var count = node.data("active_comment_count");
          // Mirror elemFor: only positive counts own a badge; a count that
          // dropped to 0 clears it instead of painting a permanent "0" chip.
          node.data("commentBadgeUrl", count > 0 ? commentBadgeDataUri(count) : "none");
        }
        if ((Number(node.data("unresolved_count")) || 0) > 0) {
          node.data("brokenBadgeUrl", brokenBadgeDataUri());
        }
      });
    }
    function syncLabelColour() {
      var pal = graphPalette();
      // Commit the complete label palette once, not a whole-graph style pass
      // for each selector. Stateful cues still follow these base rules.
      labelPrecedenceRules(cy.style().selector("node").style({
        "color": pal.nodeText,
        "border-color": pal.nodeBorder,
        // Ledger labels: --graph-label on a --graph-halo outline in every
        // palette. Light palettes need nothing more; on the dark palettes
        // light labels crossing light edges also get a square --graph-halo
        // plate at 0.85 so they stay crisp at high spread. The selected,
        // hovered or toured node's name gets an opaque --surface plate
        // (labelPrecedenceRules, appended last).
        "text-outline-color": pal.halo,
        "text-outline-width": 2,
        "text-background-color": pal.edgeLabelBg,
        "text-background-opacity": pal.dark ? 0.85 : 0
      }).selector("node.okf-hover-dim, node.dim").style({
        "color": pal.nodeText,
        "text-background-color": pal.edgeLabelBg
      }).selector("node.okf-tour-target").style({
        "border-color": pal.cue, "border-width": 5,
        "underlay-color": pal.raised, "underlay-opacity": 1, "underlay-padding": 10
      }).selector("edge").style({
        "line-color": pal.edge,
        "target-arrow-color": pal.edge,
        "text-background-color": pal.edgeLabelBg,
        "color": pal.edgeLabel
      })
      // Typed edges carry their (lens) colour in data(relColor); restore the
      // mapping a forced-colours pass replaced with a system colour.
      .selector("edge.okf-rel-edge").style({ "line-color": "data(relColor)", "target-arrow-color": "data(relColor)" }), pal).update();
      syncStatefulGraphColours(pal);
    }
      if(reducedMotionQuery){var syncGraphMotion=function(){try{cy.style().selector("node.okf-hover").style("transition-duration",REDUCED_MOTION?"0ms":"120ms").update();}catch(e){}};if(reducedMotionQuery.addEventListener)reducedMotionQuery.addEventListener("change",syncGraphMotion);else if(reducedMotionQuery.addListener)reducedMotionQuery.addListener(syncGraphMotion);}
      // ---- Forced colours: the system-colour canvas layer -----------------
      // Canvas pixels are never adjusted by the browser, so under forced
      // colours the graph paints itself from the resolved system colours
      // (systemColours). The layer is one block of rules scoped to the
      // okf-forced class, appended after every palette rule so it wins
      // Cytoscape's last-rule-wins cascade, and carried by elements only
      // while forced colours are active. Leaving forced colours removes the
      // class, so every ordinary rule (every Ledger palette value) applies
      // again exactly as before; the block is re-appended on the next entry
      // whenever palette passes have appended rules after it.
      // Hue never carries meaning here: lens categories become node shapes,
      // ordered lenses become stroke weights, and every reader state is
      // Highlight plus its own stroke width, style or ring.
      //   node          Canvas fill; CanvasText 2px solid stroke and label on
      //                 a Canvas plate
      //   lens category shape (FORCED_LENS_SHAPES; the legend draws the same)
      //   lens order    stroke 1 / 2.5 / 4 / 5.5 / 7px; undated dotted 2px
      //   orphan        dashed stroke        hub     thin CanvasText outer ring
      //   bridge        6px double stroke    dimmed  GrayText dotted 2px
      //   hover         Highlight 4px solid (plus the 15% growth)
      //   search hit    Highlight 5px dashed
      //   path node     Highlight 5px dotted
      //   focus root    Highlight 7px double (over the selected stroke)
      //   tour target   Highlight 4px solid ring, 6px outside the node
      //   presence      LinkText 3px dashed ring, 4px outside the node
      //   selected      Highlight 6px solid
      //   edge          CanvasText, 3-5px by weight; typed edges keep dashes
      //   dimmed edge   GrayText dotted 2px
      //   path edge     Highlight 7px solid; selected edge Highlight 6px dashed
      //   count chip    ButtonText on ButtonFace
      var FORCED_CLASS = "okf-forced";
      var FORCED_LENS_SHAPES = ["ellipse", "rectangle", "diamond", "triangle", "hexagon", "pentagon", "octagon", "rhomboid"];
      var FORCED_LEVEL_WIDTHS = [1, 2.5, 4, 5.5, 7];
      var forcedRulesEnd = -1, forcedRulesKey = "";
      function forcedLensCue(n) { return String(n.data("lensCue") || ""); }
      function forcedNodeShape(n) {
        var cue = forcedLensCue(n);
        if (cue.indexOf("shape-") === 0) return FORCED_LENS_SHAPES[(Number(cue.slice(6)) || 0) % FORCED_LENS_SHAPES.length];
        return n.data("shape") || "ellipse";
      }
      function forcedNodeBorderWidth(n) {
        var cue = forcedLensCue(n);
        return cue.indexOf("level-") === 0 ? (FORCED_LEVEL_WIDTHS[Number(cue.slice(6))] || 2) : 2;
      }
      function forcedNodeBorderStyle(n) { return forcedLensCue(n) === "undated" ? "dotted" : "solid"; }
      function appendForcedColourRules(sys, key) {
        var dimNodes = "node.okf-forced.dim, node.okf-forced.okf-hover-dim, node.okf-forced.okf-path-dim, node.okf-forced.okf-search-nonmatch";
        var dimEdges = "edge.okf-forced.dim, edge.okf-forced.okf-hover-dim, edge.okf-forced.okf-path-dim, edge.okf-forced.okf-unrelated";
        cy.style()
          .selector("node.okf-forced").style({
            "background-color": sys.canvas, "background-opacity": 1, "shape": forcedNodeShape,
            "border-color": sys.text, "border-opacity": 1, "border-width": forcedNodeBorderWidth, "border-style": forcedNodeBorderStyle,
            "color": sys.text, "text-outline-color": sys.canvas, "text-outline-width": 2, "text-outline-opacity": 1,
            "text-background-color": sys.canvas, "text-background-opacity": 1, "text-border-color": sys.text,
            "underlay-color": sys.canvas, "underlay-opacity": 0, "outline-width": 0, "opacity": 1
          })
          .selector("node.okf-forced.okf-orphan").style({ "border-style": "dashed" })
          .selector("node.okf-forced.okf-hub").style({ "outline-color": sys.text, "outline-style": "solid", "outline-width": 1.5, "outline-offset": 3, "outline-opacity": 1 })
          .selector("node.okf-forced.okf-bridge").style({ "border-style": "double", "border-width": 6 })
          .selector(dimNodes).style({ "border-color": sys.gray, "border-style": "dotted", "border-width": 2, "color": sys.gray, "outline-color": sys.gray })
          .selector("node.okf-forced.okf-hover").style({ "border-color": sys.highlight, "border-style": "solid", "border-width": 4 })
          .selector("node.okf-forced.okf-tour-target").style({ "outline-color": sys.highlight, "outline-style": "solid", "outline-width": 4, "outline-offset": 6, "outline-opacity": 1 })
          .selector("node.okf-forced.okf-presence-halo").style({ "outline-color": sys.link, "outline-style": "dashed", "outline-width": 3, "outline-offset": 4, "outline-opacity": 1 })
          .selector("node.okf-forced.okf-search-match").style({ "border-color": sys.highlight, "border-style": "dashed", "border-width": 5 })
          .selector("node.okf-forced.okf-path").style({ "border-color": sys.highlight, "border-style": "dotted", "border-width": 5 })
          .selector("node.okf-forced:selected").style({ "border-color": sys.highlight, "border-style": "solid", "border-width": 6 })
          // The focus root is the selection Focus explores from; it keeps its
          // own double stroke over the plain selected stroke.
          .selector("node.okf-forced.okf-focus-root").style({ "border-color": sys.highlight, "border-style": "double", "border-width": 7 })
          .selector("edge.okf-forced").style({
            "line-color": sys.text, "target-arrow-color": sys.text, "width": "mapData(weight, 0, 1, 3, 5)", "opacity": 1,
            "color": sys.text, "text-background-color": sys.canvas, "text-background-opacity": 1
          })
          .selector("edge.okf-forced.okf-multi-edge").style({
            "color": sys.buttonText, "text-background-color": sys.buttonFace, "text-border-color": sys.buttonText, "text-border-width": 1, "text-border-opacity": 1
          })
          .selector(dimEdges).style({ "line-color": sys.gray, "target-arrow-color": sys.gray, "line-style": "dotted", "width": 2, "color": sys.gray })
          .selector("edge.okf-forced.okf-path").style({ "line-color": sys.highlight, "target-arrow-color": sys.highlight, "line-style": "solid", "width": 7 })
          .selector("edge.okf-forced:selected").style({
            "line-color": sys.highlight, "target-arrow-color": sys.highlight, "line-style": "dashed", "line-dash-pattern": [12, 4], "width": 6
          })
          // A culled label stays hidden: the system plates above would
          // otherwise paint an empty box where the text yielded.
          .selector("node.okf-forced.okf-label-culled, edge.okf-forced.okf-label-culled").style(LABEL_CULLED_STYLE)
          .update();
        forcedRulesEnd = cy.style().length;
        forcedRulesKey = key;
      }
      // The forced-colours hook: theme events and forced-colour media changes
      // both route through syncGraphTheme to here. Under forced colours a
      // palette event never reaches the ordinary synchroniser; leaving them
      // hands the canvas back to it.
      function syncForcedColorGraph(){
        FORCED_COLORS=!!(forcedColorsQuery&&forcedColorsQuery.matches);
        try {
          if (FORCED_COLORS) {
            var sys = systemColours(), key = Object.keys(sys).map(function (name) { return sys[name]; }).join("|");
            // Re-append when the system colours changed or a palette pass
            // appended rules after the layer: it must stay last to win.
            if (key !== forcedRulesKey || cy.style().length !== forcedRulesEnd) appendForcedColourRules(sys, key);
            cy.elements().addClass(FORCED_CLASS);
          } else {
            cy.elements().removeClass(FORCED_CLASS);
            syncLabelColour();
          }
        } catch(e) {}
        document.body.toggleAttribute("data-okf-forced-colors",FORCED_COLORS);
      }
      // Elements added later (live refresh, LOD reveal, decluster) join the
      // system layer immediately; their glyphs already use graphInk().
      cy.on("add", function (evt) { if (FORCED_COLORS) evt.target.addClass(FORCED_CLASS); });
      // Theme events and forced-colour media changes share one route. A
      // resolved theme event must never call the ordinary synchronizer while
      // the system palette owns the graph.
    // A palette change also recolours the lens encoding: node fills (neutral
    // or the muted ramp), typed-edge hues, glyph strokes, legend and index
    // swatches and the theme-digest bars, so a lens keeps its meaning in the
    // new palette instead of keeping the previous palette's ramp.
    var lensPaintReady = false;
    // elemFor() already stamped every node with this palette's glyph and
    // neutral fill, so first paint skips the per-node pass; it runs only once
    // a palette change resolves a new token set.
    var glyphsPaintedFor = graphPalette(), glyphInkPainted = graphInk().nodeIcon;
    function refreshNodeGlyphs() {
      // The glyph ink also changes when forced colours start or stop (or the
      // system colours change) while the Ledger palette stays the same.
      var pal = graphPalette(), ink = graphInk().nodeIcon;
      if (pal === glyphsPaintedFor && ink === glyphInkPainted) return;
      glyphsPaintedFor = pal;
      glyphInkPainted = ink;
      var neutral = pal.node;
      cy.batch(function () {
        cy.nodes().forEach(function (node) {
          node.data("iconUrl", iconDataUri(node.data("icon") || "file"));
          node.data("baseColor", neutral);
        });
      });
    }
    function repaintLensColours() {
      refreshNodeGlyphs();
      if (!lensPaintReady) return;
      // Recolours nodes and typed edges, then legend, index swatches, the
      // orphan shelf and the theme-digest bars from the new palette.
      applyVisualEncoding();
    }
    function syncGraphTheme(event){
      if(forcedColorsQuery)FORCED_COLORS=!!forcedColorsQuery.matches;
      // A palette event invalidates the resolved token set; a forced-colours
      // media change keeps it (custom properties are not forced).
      if(event&&event.type==="okf-loom:themeChanged")invalidateGraphPalette();
      // System colours are re-resolved on every pass: a forced-colour theme
      // or colour-scheme switch changes them without a palette event.
      invalidateSystemColours();
      cy.batch(function () {
        syncBadgeDataUris();
        repaintLensColours();
        // Both directions go through the forced-colours hook: entering
        // applies the system layer, leaving removes it and re-runs the
        // ordinary label/state synchroniser.
        syncForcedColorGraph();
      });
      syncMinimap();
      }
      window.addEventListener("okf-loom:themeChanged",syncGraphTheme);
      if(forcedColorsQuery){if(forcedColorsQuery.addEventListener)forcedColorsQuery.addEventListener("change",syncGraphTheme);else if(forcedColorsQuery.addListener)forcedColorsQuery.addListener(syncGraphTheme);}
      // A high-contrast theme switch can change the system colours while
      // forced colours stay active; outside forced colours theme.js owns
      // the System appearance and announces it with a palette event.
      var forcedSchemeQuery=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)");
      if(forcedSchemeQuery){var syncForcedScheme=function(){if(FORCED_COLORS)syncGraphTheme();};if(forcedSchemeQuery.addEventListener)forcedSchemeQuery.addEventListener("change",syncForcedScheme);else if(forcedSchemeQuery.addListener)forcedSchemeQuery.addListener(syncForcedScheme);}
      syncGraphTheme();

    // Legend overlay. Rebuilt by updateLegend() so it reflects the
    // active "Colour by" mode (type palette, per-group colours, or an
    // importance ramp) — never a stale key that disagrees with the canvas.
    var legend = document.createElement("div");
    legend.className = "okf-graph-legend";
    legend.setAttribute("aria-label", "Graph colour key");
    container.appendChild(legend);

    // Phase 3: deterministic labelled orphan shelf. Nodes with bundle-level
    // degree 0 are docked into a labelled bottom tray ("Not yet linked — N")
    // so they read as a visible authoring TODO instead of floating in space.
    // The fcose layout already tiles degree-zero nodes deterministically;
    // this shelf is the LABELLED overlay that makes the tray legible + a
    // keyboard-accessible equivalent to the node index for that subset.
    // Rebuilt by refreshOrphanShelf() after every applyVisualEncoding pass
    // (orphan set is a read-only derivation from degreeOf, so there is no
    // state to own beyond the live recompute — state-ownership invariant).
    // Built entirely with createElement + textContent (CSP-safe, no inline
    // markup, no innerHTML). The shelf is hidden when the set is empty
    // (most non-trivial bundles have no orphans) and on mobile-collapsed
    // where the bottom edge is already a touch-target zone.
    var orphanShelf = document.createElement("div");
    orphanShelf.className = "okf-graph-orphans";
    orphanShelf.setAttribute("role", "region");
    orphanShelf.setAttribute("aria-label", "Concepts with no links yet");
    orphanShelf.hidden = true;
    container.appendChild(orphanShelf);
    function refreshOrphanShelf() {
      // Deterministic orphan set: degree-zero + currently shown nodes,
        // sorted by (label, id) so the shelf order is byte-stable across
        // reloads / payload revisions (layout-graph determinism §3.2).
        var orphanNodes = bundle.nodes.filter(function (n) {
          return !!shownIds[n.data.id] && (degreeOf[n.data.id] || 0) === 0;
        }).sort(function (a, b) {
          var la = String(a.data.label || a.data.id || "").toLowerCase();
          var lb = String(b.data.label || b.data.id || "").toLowerCase();
          if (la < lb) return -1;
          if (la > lb) return 1;
          var ia = String(a.data.id), ib = String(b.data.id);
          return ia < ib ? -1 : (ia > ib ? 1 : 0);
        });
        while (orphanShelf.firstChild) orphanShelf.removeChild(orphanShelf.firstChild);
        if (!orphanNodes.length) {
          orphanShelf.hidden = true;
          if (container) container.classList.remove("okf-graph-has-orphans");
          return;
        }
        orphanShelf.hidden = false;
        // Mark the canvas so the (collapsed) legend lifts out of the
        // bottom-edge zone the shelf now claims — see graph.css.
        if (container) container.classList.add("okf-graph-has-orphans");
        var head = document.createElement("div");
        head.className = "okf-graph-orphans__head";
        var title = document.createElement("span");
        title.className = "okf-graph-orphans__title";
        // Deterministic count copy: "Not yet linked — N" (singular/plural
        // kept simple; the count is the contract, not the prose).
        title.textContent = "Not yet linked \u2014 " + orphanNodes.length;
        head.appendChild(title);
        var hint = document.createElement("span");
        hint.className = "okf-graph-orphans__hint okf-muted";
        hint.textContent = "These concepts have no relationships. Add one to surface them in the graph.";
        head.appendChild(hint);
        orphanShelf.appendChild(head);
        var list = document.createElement("div");
        list.className = "okf-graph-orphans__list";
        list.setAttribute("role","listbox");list.setAttribute("aria-label","Concepts with no relationships");list.tabIndex=0;
        orphanNodes.forEach(function (n,optionIndex) {
          var id = n.data.id;
          var label = n.data.label || id;
          var btn = document.createElement("div");
          btn.className = "okf-node-index__item okf-graph-orphans__item";
          btn.id="okf-orphan-option-"+optionIndex;btn.tabIndex=-1;btn.setAttribute("role","option");btn.setAttribute("aria-selected","false");
          btn.setAttribute("data-target", id);
          btn.setAttribute("aria-label", label + " (not yet linked)");
          var sw = document.createElement("span");
          sw.className = "okf-node-index__swatch";
          var shelfNode = cy.getElementById(id);
          sw.style.background = (shelfNode && shelfNode.length && shelfNode.data("color")) || graphPalette().node;
          setForcedCue(sw, shelfNode && shelfNode.length ? shelfNode.data("lensCue") : "");
          btn.appendChild(sw);
          var lbl = document.createElement("span");
          lbl.className = "okf-node-index__label";
          lbl.textContent = label;
          btn.appendChild(lbl);
          btn.addEventListener("keydown", function (e) {
            if (e.key === "Enter" || e.key === " ") btn.dataset.keyboardActivation = "1";
          });
          btn.addEventListener("click", function (e) {
            e.preventDefault();
            var keyboard = e.detail === 0 || btn.dataset.keyboardActivation === "1";
            delete btn.dataset.keyboardActivation;
            dispatchInteraction({type:"SELECT",id:id,source:keyboard?"index-keyboard":"index-pointer"});
          });
          list.appendChild(btn);
        });
        function orphanOptions(){return Array.prototype.slice.call(list.querySelectorAll('[role=option]'));}
        function activateOrphan(index){var rows=orphanOptions();index=Math.max(0,Math.min(rows.length-1,index));var row=rows[index];list.setAttribute("aria-activedescendant",row.id);rows.forEach(function(item){item.classList.toggle("is-active",item===row);});dispatchInteraction({type:"ACTIVE",id:row.dataset.target,quietUrl:true});row.scrollIntoView({block:"nearest"});}
        list.addEventListener("focus",function(){activateOrphan(0);});
        list.addEventListener("keydown",function(event){var rows=orphanOptions(),current=list.getAttribute("aria-activedescendant"),index=rows.findIndex(function(row){return row.id===current;});if(index<0)index=0;if(event.key==="ArrowDown"||event.key==="ArrowRight"){event.preventDefault();activateOrphan(index+1);}else if(event.key==="ArrowUp"||event.key==="ArrowLeft"){event.preventDefault();activateOrphan(index-1);}else if(event.key==="Home"){event.preventDefault();activateOrphan(0);}else if(event.key==="End"){event.preventDefault();activateOrphan(rows.length-1);}else if(event.key===" "){event.preventDefault();var id=rows[index].dataset.target;if(event.shiftKey)dispatchInteraction({type:"PATH",id:id});else dispatchInteraction({type:"SELECT",id:id,source:"index-keyboard"});}else if(event.key==="Enter"){event.preventDefault();openConceptPage(rows[index].dataset.target);}else if(event.key==="Escape"){event.preventDefault();clearLocalInteraction();}});
        var firstOrphan=list.querySelector('[role=option]');if(firstOrphan)list.setAttribute("aria-activedescendant",firstOrphan.id);
        orphanShelf.appendChild(list);
      }
    // A legend row: a square swatch in the canvas colour it explains (the
    // muted lens ramp), or a hollow canvas-coloured square for "no data".
    // `cue` is the row's forced-colours key (shape or stroke weight).
    function legendRow(swatchBg, text, cue) {
      var item = document.createElement("div");
      item.className = "okf-graph-legend__item";
      var sw = document.createElement("span");
      sw.className = "okf-graph-legend__swatch";
      if (swatchBg === "transparent") sw.classList.add("okf-graph-legend__swatch--none");
      else sw.style.background = swatchBg;
      sw.dataset.swatch = swatchBg;
      setForcedCue(sw, cue);
      var label = document.createElement("span");
      label.textContent = text;
      item.appendChild(sw); item.appendChild(label);
      return item;
    }
      // P1 graph/type collision fix: a type FILTER chip that encodes type via
      // ICON + text ONLY (no colour swatch). Used in the "Filter types"
      // section so the legend never presents type colours alongside theme
      // colours — one colour variable, one meaning. The toggle/solo behaviour
      // is the type filter's single chip, so keyboard + click flows are
      // identical in every lens.
      function legendTypeFilterChip(t) {
        var item = document.createElement("button");item.type="button";
        item.className = "okf-graph-legend__item okf-graph-legend__item--chip okf-graph-legend__item--type-filter okf-filter-chip";item.dataset.filterValue=t;
        item.innerHTML = typeIconSvgMarkup(t);
        var label = document.createElement("span");
        label.textContent = t;
        item.appendChild(label);
        var off = !!controlState.hiddenTypes[t];
        item.classList.toggle("okf-graph-legend__item--off", off);
        item.setAttribute("aria-pressed", String(!off));
        item.title = "Click: show/hide " + t + " · Alt-click: only " + t;
        function toggle(alt) {
          var ht = controlState.hiddenTypes,candidate=Object.assign({},ht);
          if (alt) {
            var soloed = !ht[t] && Object.keys(palette).some(function (o) { return o !== t && ht[o]; })
              && Object.keys(palette).every(function (o) { return o === t ? !ht[o] : ht[o]; });
            candidate={};
            if (!soloed) {
              Object.keys(palette).forEach(function (o) { if (o !== t) candidate[o] = true; });
            }
          } else {
            candidate[t] = !candidate[t];
          }
          if(!commitHiddenFilterState("Type",ht,candidate))return;
          applyFilters();
          updateLegend();
          updateStatus();
          writeInteractionUrl("replace");syncMinimap();
        }
        item.addEventListener("click", function (e) { toggle(e.altKey); });
        return item;
      }
    // with a dash variant matching the canvas dash class.
    function legendLineRow(color, dashClass, text) {
      var item = document.createElement("div");
      item.className = "okf-graph-legend__item";
      var line = document.createElement("span");
      line.className = "okf-graph-legend__line okf-legend-" + dashClass;
      line.style.borderTopColor = color;
      var label = document.createElement("span");
      label.textContent = text;
      item.appendChild(line); item.appendChild(label);
      return item;
    }
    function legendTitle(text) {
      var t = document.createElement("div");
      t.className = "okf-graph-legend__title";
      t.textContent = text;
      return t;
    }
    // Phase 3 P1: the legend is COLLAPSIBLE so a dense canvas is not
    // obscured by a tall type+filter+relation key. Collapsed by default
    // (compact toggle button); expanding is an explicit user action and the
    // expanded panel may overlap the graph until collapsed again. The flag
    // survives updateLegend rebuilds (lens/colour-mode changes) so a user
    // who expanded the key does not have it snap shut on a lens switch.
    var legendCollapsed = true;
    function updateLegend(restoreRelationshipFocus) {
      var active=document.activeElement,focusSolo=!!(active&&active.classList&&active.classList.contains("okf-filter-solo")),focusValue=active&&legend.contains(active)?(focusSolo?active.dataset.filterSoloValue:active.dataset.filterValue):null,focusKind=active&&active.closest&&active.closest('[aria-label="Relationship filters"]')?"Relationship":"Type";
      while (legend.firstChild) legend.removeChild(legend.firstChild);
      var body = document.createElement("div");
      body.className = "okf-graph-legend__body";
      // Node colour key — driven by the active lens's colour mode.
        if (controlState.colorMode === "community") {
          var com = computeCommunities();
          var pr = computePageRank();
          body.appendChild(legendTitle(lensKeyTitle("shape", "themes")));
          com.list.slice(0, 8).forEach(function (c) {
            if (!c.ids.length) return;
            var ex = c.ids.slice().sort(function (a, b) { return (pr[b] || 0) - (pr[a] || 0); })[0];
            var d = nodeIndex[ex] || {};
            // P1 collision fix: textual marker (T1, T2…) provides non-colour
            // theme identity so the legend is readable without relying on the
            // theme palette alone.
            body.appendChild(legendRow(communityColor(c.index),
              themeLabel(c.index) + " \u00b7 " + (d.label || ex), lensShapeCue(c.index)));
          });
          if (com.list.length > 8) body.appendChild(legendRow("transparent", "+ " + (com.list.length - 8) + " more"));
      } else if (controlState.colorMode === "recency") {
          body.appendChild(legendTitle(lensKeyTitle("level", "freshness")));
          body.appendChild(legendRow(recencyColor(7), "This month", recencyCue(7)));
          body.appendChild(legendRow(recencyColor(90), "This quarter", recencyCue(90)));
          body.appendChild(legendRow(recencyColor(365), "6 months +", recencyCue(365)));
          var undatedRow = legendRow(recencyColor(Infinity), "No timestamp", recencyCue(Infinity));
          undatedRow.firstChild.classList.add("okf-graph-legend__swatch--hollow");
          body.appendChild(undatedRow);
      } else if (controlState.colorMode === "bridge") {
          body.appendChild(legendTitle(lensKeyTitle("level", "bridging power")));
          body.appendChild(legendRow(bridgeColor(0.1), "On few paths", lensLevelCue(0.1)));
          body.appendChild(legendRow(bridgeColor(0.6), "Connective", lensLevelCue(0.6)));
          body.appendChild(legendRow(bridgeColor(1), "Holds areas together", lensLevelCue(1)));
      } else if (controlState.colorBy === "group" && controlState.groupBy !== "type") {
          body.appendChild(legendTitle(lensKeyTitle("shape", GROUP_LABELS[controlState.groupBy])));
          groupList.slice(0, 12).forEach(function (g) { body.appendChild(legendRow(groupColor(g), g, groupCue(g))); });
          if (groupList.length > 12) body.appendChild(legendRow("transparent", "+ " + (groupList.length - 12) + " more"));
      } else if (controlState.colorBy === "importance") {
          body.appendChild(legendTitle("Node " + (FORCED_COLORS ? "outline" : "colour") + ": importance (" + SIGNAL_LABELS[controlState.primarySignal] + ")"));
          body.appendChild(legendRow(importanceColor(0.1), "Low", lensLevelCue(0.1)));
          body.appendChild(legendRow(importanceColor(0.5), "Medium", lensLevelCue(0.5)));
          body.appendChild(legendRow(importanceColor(0.95), "High", lensLevelCue(0.95)));
      }
        // Without a colour lens the nodes are neutral (content types are not
        // colour-coded), so there is no colour key; types stay filterable
        // through the icon chips below in every mode. The "Filter types"
        // section shows type ICONS (non-colour encoding), never swatches.
        {
          // Keep the title and filter rows under one semantic owner.  The
          // established legend contract also requires rows to be direct title
          // siblings (capture tooling deliberately does no wrapper traversal).
          var iconTypeFilters=document.createElement("div");iconTypeFilters.className="okf-filter-chips";iconTypeFilters.setAttribute("aria-label","Type filters");
          iconTypeFilters.appendChild(legendTitle("Filter types"));
          Object.keys(palette).sort().forEach(function (t) {
            var chip=legendTypeFilterChip(t),count=bundle.nodes.filter(function(n){return n.data.type===t;}).length;
            chip.querySelector("span:last-child").textContent=t+" ("+count+")";
            iconTypeFilters.appendChild(chip);
            var solo=document.createElement("button");solo.type="button";solo.className="okf-filter-solo";solo.dataset.filterSoloValue=t;solo.setAttribute("aria-label","Show only "+t);solo.textContent="Only";
            solo.addEventListener("click",function(){var candidate={};Object.keys(palette).forEach(function(v){candidate[v]=v!==t;});if(!commitHiddenFilterState("Type",controlState.hiddenTypes,candidate))return;applyFilters();updateLegend();updateStatus();writeInteractionUrl("replace");syncMinimap();});
            iconTypeFilters.appendChild(solo);
          });
          var showTypes=document.createElement("button");showTypes.type="button";showTypes.className="okf-filter-showall";showTypes.textContent="Show all";showTypes.addEventListener("click",function(){replaceRecord(controlState.hiddenTypes,{});applyFilters();updateLegend();updateStatus();writeInteractionUrl("replace");syncMinimap();});iconTypeFilters.appendChild(showTypes);body.appendChild(iconTypeFilters);
        }
      // Relation-type EDGE key (reviewer blocker 6 / phase 3 dash-always):
      // the canvas ALWAYS encodes typed edges by hue + dash, so the legend
      // must ALWAYS explain that mapping when labelled edges exist —
      // otherwise the legend contradicts the canvas. Previously this was
      // gated on the Relations preset, which now disagrees with the
      // always-on encoding.
      // One row per relation type the canvas actually draws (each edge's
      // primary type), busiest first, in that type's real line style: plain
      // links solid, typed relations dashed (hued only under Relations).
      var drawnTypes = {};
      cy.edges().forEach(function (e) { var t = e.data("relType"); if (t) drawnTypes[t] = (drawnTypes[t] || 0) + 1; });
      var drawnTypeList = Object.keys(drawnTypes).sort(function (a, b) { return (drawnTypes[b] - drawnTypes[a]) || (a < b ? -1 : (a > b ? 1 : 0)); });
      if (drawnTypeList.length) {
        body.appendChild(legendTitle("Edge: relation type"));
        drawnTypeList.slice(0, 10).forEach(function (type) {
          var typed = isTypedRelation(type);
          var row = legendLineRow(typed ? relColor(type, !!controlState.relationEdges) : graphPalette().edge, typed ? relDashClass(type) : "solid", type);
          row.dataset.relationType = type;
          body.appendChild(row);
        });
        if (drawnTypeList.length > 10) body.appendChild(legendRow("transparent", "+ " + (drawnTypeList.length - 10) + " more relation types"));
      }
      if (relationLabelList.length) buildFilterChips(body,allRelationValues(),controlState.hiddenRelations,"Relationship");
      var hasBody = body.childNodes.length > 0;
      legend.hidden = !hasBody;
      if (!hasBody) { legend.classList.remove("okf-graph-legend--collapsed"); return; }
      body.id = "okf-graph-legend-body";
      legend.appendChild(body);
      legend.classList.remove("okf-graph-legend--collapsed");
      if(!focusValue&&restoreRelationshipFocus&&document.body.dataset.okfRelationshipFilterFocus){focusValue=document.body.dataset.okfRelationshipFilterFocus;focusSolo=document.body.dataset.okfRelationshipFilterFocusSolo==="1";focusKind="Relationship";}if(focusValue){var owner=legend.querySelector('[aria-label="'+focusKind+' filters"]'),base=owner&&owner.querySelector('[data-filter-value="'+CSS.escape(focusValue)+'"]'),target=focusSolo&&base?base.parentElement.querySelector('.okf-filter-solo'):base;if(target)target.focus();}
    }

    // iter2 CRI2-006: LOD "Show all" pill. Rendered only when the bundle
    // exceeded GRAPH_LOD_THRESHOLD and the hidden set is non-empty. Clicking
    // streams the remaining nodes + edges in one pass and switches to a grid
    // layout (cose on N>100 is the jank source). The pill is an accessible
    // <button> with an aria-label that names the full count.
    function refreshLodPill() {
      var hidden = 0;
      bundle.nodes.forEach(function (n) { if (!shownIds[n.data.id]) hidden++; });
      if (!lodActive || hidden === 0) {
        if (lodPill && lodPill.parentNode) lodPill.parentNode.removeChild(lodPill);
        lodPill = null;
        return;
      }
      if (!lodPill) {
        lodPill = document.createElement("button");
        lodPill.type = "button";
        lodPill.className = "okf-graph-lod-pill";
        if (container) container.appendChild(lodPill);
        lodPill.addEventListener("click", showAllGraph);
      }
      var shownCount = totalNodeCount - hidden;
      lodPill.textContent = "Showing " + shownCount + " of " + totalNodeCount + " nodes. Show all";
      lodPill.setAttribute("aria-label",
        "Showing " + shownCount + " of " + totalNodeCount + " nodes. Show every node (switches to a faster grid layout).");
    }
    // ensureNodeVisible(id): decluster-on-focus. If the requested node is not
    // currently in the canvas (LOD hid it), add it + any edges to already-
    // shown nodes, then re-run the layout so the new node is placed. Used by
    // showDetail (keyboard index + tap) and applyPresenceFocus (the spatial
    // halo) so the agent's focus is always visible even on a clustered graph.
    function ensureNodeVisible(id) {
      if (!id || shownIds[id]) return false;
      if (!nodeIndex[id]) return false;
      shownIds[id] = true;
      var nodeBundle = null;
      for (var i = 0; i < bundle.nodes.length; i++) {
        if (bundle.nodes[i].data.id === id) { nodeBundle = bundle.nodes[i]; break; }
      }
      if (nodeBundle) cy.add(elemFor(nodeBundle));
      // Add edges that now have both endpoints visible (either direction).
      bundle.edges.forEach(function (e) {
        if (!edgeVisible(e)) return;
        var key = e.data.id;
        if (seenEdge[key]) return;
        seenEdge[key] = 1;
        var edgeData = edgeElementData(e,0.5); edgeData.id=key;
        cy.add({ data: edgeData });
      });
      // The new node/edges inherit the current weighting/colour/filter state.
      applyVisualEncoding();
      applyFilters();
      applyEdgeElision();  // P1 progressive disclosure: elide newly added edges
      // Re-run the current layout THROUGH the single stale-safe owner so this
      // direct (non-debounced) path also stops any in-flight layout, bumps
      // layoutSeq, and fences its fit. Uses the EXPLICIT currentLayoutName
      // (cy.layoutName is not a guaranteed Cytoscape field). This replaces the
      // former direct cy.layout(...).run() + synchronous postLayout() that
      // bypassed the owner (async-lifecycle blocker).
      runLayoutNow(false);
      updateStatus();
      refreshLodPill();
      return true;
    }
    function showAllGraph() {
      // Stream every hidden node + edge in one pass, then switch to a faster
      // layout. Grid is O(n) and pan/zoom-friendly; cose on 500 nodes is the
      // jank source we are explicitly avoiding. Animate off under the reduced-
      // motion media query (graph.js honors REDUCED_MOTION globally).
      var added = [];
      bundle.nodes.forEach(function (n) {
        if (shownIds[n.data.id]) return;
        shownIds[n.data.id] = true;
        added.push(elemFor(n));
      });
      if (added.length) cy.add(added);
      bundle.edges.forEach(function (e) {
        if (!edgeVisible(e)) return;
        var key = e.data.id;
        if (seenEdge[key]) return;
        seenEdge[key] = 1;
        var edgeData = edgeElementData(e,0.5); edgeData.id=key;
        cy.add({ data: edgeData });
      });
      // Switch to grid (fast, O(n)) and keep the control state in sync so the
      // topbar select + Signal status reflect the actual layout now on screen.
      currentLayoutName = "grid";
      controlState.layout = "grid";
      if (layoutSel) layoutSel.value = "grid";
        lodActive = false;  // user explicitly asked for all; no more auto-LOD
        lodShowAllExplicit = true;
      // The full node/edge set inherits the current weighting/colour/filters.
      applyVisualEncoding();
      applyFilters();
      applyEdgeElision();  // P1 progressive disclosure: elide newly streamed edges
      // Layout THROUGH the single stale-safe owner (was a direct cy.layout run
      // that bypassed layoutSeq/runningLayout — async-lifecycle blocker).
      runLayoutNow(false);
      updateStatus();
      refreshLodPill();
    }
    refreshLodPill();

    // P1-4 / P2-2 (iter-1): keyboard-accessible node index. Populates the
    // <details class="okf-node-index"> overlay with one <button> per node,
    // sorted by label (determinism §3.2). Each button calls showDetail(id)
    // - the same handler Cytoscape's tap uses - so keyboard users get full
    // graph access (WCAG 2.1.1).
    buildNodeIndex(bundle);

    // ====================================================================
    // Signal controls — apply layer (style / filter / layout / status)
    // ====================================================================

    // Re-derive node size/colour, edge weight, and bridge cues from the
    // current control state. Writes go through data() + class toggles inside
    // cy.batch() so the stylesheet's mapData()/class selectors keep ownership
    // (an inline ele.style bypass would clobber .dim and :selected).
    function applyVisualEncoding() {
      // Lens encodings — computed lazily by applyLens, read here.
      var pr = controlState.sizeMode === "pagerank" ? computePageRank() : null;
      var bt = (controlState.sizeMode === "betweenness" || controlState.colorMode === "bridge" ||
                controlState.showBridges) ? computeBetweenness() : null;
      if (controlState.colorMode === "community" || controlState.groupBy === "community") computeCommunities();
      cy.batch(function () {
        cy.nodes().forEach(function (n) {
          var id = n.id();
          var vs = n.data("size");
          if (controlState.sizeMode === "pagerank" && pr) vs = 24 + 44 * (pr[id] || 0);
          else if (controlState.sizeMode === "betweenness" && bt) vs = 22 + 50 * (bt[id] || 0);
          else if (controlState.scaleByImportance) vs = 26 + 44 * nodeWeight(id);
          n.data("vizSize", vs);
          // lensCue is the same encoding without hue (shape or stroke
          // weight); only the forced-colours layer and its keys read it.
          var col = n.data("baseColor"), cue = "";
          if (controlState.colorMode === "community") { col = communityColor(communityOf[id] || 0); cue = lensShapeCue(communityOf[id] || 0); }
          else if (controlState.colorMode === "recency") { var days = recencyDays(id); col = recencyColor(days); cue = recencyCue(days); }
          else if (controlState.colorMode === "bridge" && bt) { col = bridgeColor(bt[id] || 0); cue = lensLevelCue(bt[id] || 0); }
          else if (controlState.colorBy === "group") { col = groupColor(groupOf[id]); cue = groupCue(groupOf[id]); }
          else if (controlState.colorBy === "importance") { col = importanceColor(nodeWeight(id)); cue = lensLevelCue(nodeWeight(id)); }
          n.data("color", col);
          n.data("lensCue", cue);
          // Bridges: the glow marks the TOP bridgers by real betweenness
          // (was: heuristic cross-group edge counting).
          n.toggleClass("okf-bridge", !!(controlState.showBridges && bt && (bt[id] || 0) >= BRIDGE_CUT));
          // Phase 3: degree-zero concepts carry the "not yet linked" cue.
          n.toggleClass("okf-orphan", n.degree(false) === 0);
          // Phase 3: top-decile hubs get a soft accent halo. The halo is
          // derived SOLELY from degree rank (read-only derivation from
          // degreeOf) so it never competes with the user-driven bridge /
          // focus halos — those are applied via stronger z-indexed
          // underlays. Hub-ness is structural and lens-independent.
          n.toggleClass("okf-hub", (degreeOf[id] || 0) > 0 && (degreeOf[id] || 0) >= HUB_DEGREE_THRESHOLD);
          // Phase 3: broken-link indicator. The server ships a deterministic
          // per-node ``unresolved_count`` derived from Graph.unresolved
          // (SPEC §5.3); expose it as a tight red underlay ring on the
          // nodes that own unresolved links so the authoring TODO is
          // visible at a glance. Non-colour channels (tooltip, detail
          // panel, node index) carry the same information for a11y.
          // Read from the cy node data (set by elemFor) so this stays
          // correct after ensureNodeVisible / live refresh add nodes.
          var unresolved = Number(n.data("unresolved_count")) || 0;
          n.toggleClass("okf-broken-link", unresolved > 0);
        });
        cy.edges().forEach(function (e) {
          e.data("weight", edgeWeight(e.id()));
          e.toggleClass("okf-multi-edge", Number(e.data("multiplicity")) > 1);
          var cross = controlState.groupBy !== "none" && groupOf[e.source().id()] !== groupOf[e.target().id()];
          e.toggleClass("okf-bridge-edge", controlState.showBridges && cross);
          // Relation-type encoding on the edge (colour + dash). Phase 3:
          // typed edges ALWAYS carry their hue + dash pattern — muted by
          // default, full saturation under the explicit Relations intent.
          // Dash-always-on lets users spot relationship categories in the
          // default Map lens without having to flip presets (the prior
          // gate was a reviewer-6 contradiction with the plan's "typed
          // edge get their hue/dash always" requirement).
          var relType = edgePrimaryType(e.data());
          e.data("relType", relType);
          e.removeClass("okf-rel-edge okf-reld-1 okf-reld-2 okf-reld-3");
          if (isTypedRelation(relType)) {
            e.data("relColor", relColor(relType, !!controlState.relationEdges));
            e.addClass("okf-rel-edge");
            e.addClass(relDashClass(relType));
          }
        });
      });
      applyEdgeLabels();
      scheduleLabelCull();
      updateLegend();
      updateNodeIndexSwatches();
      // Phase 3: orphan shelf is a deterministic derivation from degreeOf,
      // recomputed every visual-encoding pass so it stays in lockstep with
      // the canvas (no stale entries after a lens switch, LOD show-all,
      // or live refresh). degreeOf is bundle-level (unique neighbours) and
      // is unaffected by edge elision / filters, so the shelf reflects
      // STRUCTURAL orphans, not "currently-filtered-out" nodes.
      refreshOrphanShelf();
      syncLensSummaryColours();
    }

    // Keep the theme digest's 2px bars in step with the canvas: the ramp
    // colour while themes colour the nodes, the neutral rule otherwise.
    function syncLensSummaryColours() {
      var on = controlState.colorMode === "community";
      Array.prototype.forEach.call(document.querySelectorAll("#detail-empty .okf-lens-summary__bar[data-community]"), function (bar) {
        if (on) bar.style.background = communityColor(Number(bar.getAttribute("data-community")) || 0);
        else bar.style.removeProperty("background");
      });
    }
    // The starting point a reader picked stays marked (aria-current + the
    // 2px accent edge) until the selection moves or clears.
    function syncLensSummaryCurrent() {
      Array.prototype.forEach.call(document.querySelectorAll("#detail-empty .okf-lens-summary__row[data-target]"), function (rowEl) {
        if (selectedConceptId && rowEl.getAttribute("data-target") === selectedConceptId) rowEl.setAttribute("aria-current", "true");
        else rowEl.removeAttribute("aria-current");
      });
    }
    window.addEventListener("okf-loom:graph-selection", syncLensSummaryCurrent);

    // Toggle the per-edge `.okf-show-label` class. Labels show in Relations/
    // Focus presets (showEdgeLabels) and for edges touching the currently-
    // selected node, so context appears without global clutter.
    //
    // Review feedback: the IMPLICIT selected-node labels are suppressed once
    // Node spacing is high (index >= 3, Wide/Vast). At that spread the auto-fit
    // zooms the graph out so far that those rotated relationship labels render
    // sub-legible and read as faint noise (the reviewer's edge-label P1). Hiding
    // them at high spread keeps the canvas clean; they return as soon as spacing
    // drops or the user zooms/selects in. The EXPLICIT Relations/Focus path
    // (showEdgeLabels) is untouched — that is deliberate "show the relationships"
    // intent and stays on the existing min-zoomed-font-size LOD.
      // D2-010: relationship text is a neighbourhood reading aid, never a
      // whole-map layer. With labels on (the setting, or a lens that asks
      // for them) only the edges of the selected, hovered or focus-root
      // concept carry text; every other edge stays a quiet line. Painting
      // all of them at once was an unreadable pile of rotated anchor text.
      function applyEdgeLabels() {
        var selId = null;
        var sel = cy.nodes(":selected");
        if (sel && sel.length) selId = sel.id();
        var labelOwners = {};
        if (selId) labelOwners[selId] = true;
        if (interactionState.hovered) labelOwners[interactionState.hovered] = true;
        if (controlState.focusEnabled && focusRoot) labelOwners[focusRoot] = true;
        // Relationship text is an explicit Advanced choice. Selection still
        // emphasizes incident lines, but never silently turns labels on.
        // P1 edge highlight: toggle .okf-incident on edges touching the
        // selected node in the same batch pass as label toggling so both
        // prominence cues apply atomically. clearSelection runs the same
        // function (selId=null) so every incident class is removed on deselect.
        cy.batch(function () {
          cy.edges().forEach(function (e) {
            var incident = !!(selId && (e.source().id() === selId || e.target().id() === selId));
            e.toggleClass("okf-incident", incident);
            var show = false;
            if (e.data("label") && controlState.showEdgeLabels && !FORCED_COLORS) {
              show = !!(labelOwners[e.source().id()] || labelOwners[e.target().id()]);
            }
            e.toggleClass("okf-show-label", show);
          });
        });
      }

    // Label culling (D2-006). Labels claim their painted box (plate and
    // halo included) in priority order and a label or count chip that would
    // overprint an already claimed box is hidden for now:
    //   3  the selected, hovered or toured concept (the reader's focus)
    //   2  focus root, search hits and path stops (explicit reader cues)
    //   1  every other concept, busiest first (degree, then id)
    //   -  bundled-relationship count chips (the bundle list keeps the count)
    //   0  dimmed concepts (filtered, faded by hover/path/search)
    // Label sizes are model units, so overlap does not change with zoom:
    // the pass reruns when positions, classes or label styles change, never
    // per zoom frame. Culling only toggles text opacity, never geometry.
    var labelCullFrame = 0;
    function scheduleLabelCull() {
      if (labelCullFrame) return;
      labelCullFrame = window.requestAnimationFrame(function () {
        labelCullFrame = 0;
        if (!cy.destroyed()) cullOverlappingLabels();
      });
    }
    var LABEL_BOX = { includeNodes: false, includeEdges: false, includeLabels: true, includeOverlays: false };
    function labelPriority(n) {
      if (n.selected() || n.hasClass("okf-hover") || n.hasClass("okf-tour-target")) return 3;
      if (n.hasClass("dim") || n.hasClass("okf-hover-dim") || n.hasClass("okf-path-dim") || n.hasClass("okf-search-nonmatch")) return 0;
      if (n.hasClass("okf-focus-root") || n.hasClass("okf-search-match") || n.hasClass("okf-path")) return 2;
      return 1;
    }
    function cullOverlappingLabels() {
      var CELL = 96, grid = {}, culled = {};
      function visit(b, fn) {
        for (var gx = Math.floor(b.x1 / CELL); gx <= Math.floor(b.x2 / CELL); gx++)
          for (var gy = Math.floor(b.y1 / CELL); gy <= Math.floor(b.y2 / CELL); gy++)
            if (fn(gx + ":" + gy)) return true;
        return false;
      }
      function claim(ele) {
        var bb = ele.boundingBox(LABEL_BOX);
        if (!(bb.w > 0 && bb.h > 0)) return;
        // Cytoscape's node label box adds the halo, plate padding and margins
        // (about 6 units around 13px text); touching halos are not an
        // overprint, so only the inked text box claims space. A count chip's
        // opaque plate is what overprints, so it claims its whole box.
        var inset = ele.isNode() ? 6 : 1;
        var b = { x1: bb.x1 + inset, y1: bb.y1 + inset, x2: bb.x2 - inset, y2: bb.y2 - inset };
        if (b.x2 <= b.x1 || b.y2 <= b.y1) b = { x1: bb.x1, y1: bb.y1, x2: bb.x2, y2: bb.y2 };
        var hit = visit(b, function (key) {
          var list = grid[key];
          if (!list) return false;
          for (var i = 0; i < list.length; i++) {
            var o = list[i];
            if (b.x1 < o.x2 && o.x1 < b.x2 && b.y1 < o.y2 && o.y1 < b.y2) return true;
          }
          return false;
        });
        if (hit) { culled[ele.id()] = true; return; }
        visit(b, function (key) { (grid[key] || (grid[key] = [])).push(b); return false; });
      }
      var labelled = cy.nodes(":visible").filter(function (n) { return !!n.style("label"); }).toArray();
      labelled = labelled.map(function (n) { return { n: n, p: labelPriority(n), d: degreeOf[n.id()] || 0, id: n.id() }; });
      labelled.sort(function (a, b) { return (b.p - a.p) || (b.d - a.d) || (a.id < b.id ? -1 : (a.id > b.id ? 1 : 0)); });
      var chips = cy.edges(".okf-multi-edge:visible").toArray();
      var i = 0;
      for (; i < labelled.length && labelled[i].p > 0; i++) claim(labelled[i].n);
      chips.forEach(claim);
      for (; i < labelled.length; i++) claim(labelled[i].n);
      cy.batch(function () {
        cy.elements(".okf-label-culled").forEach(function (ele) { if (!culled[ele.id()]) ele.removeClass("okf-label-culled"); });
        Object.keys(culled).forEach(function (id) { cy.getElementById(id).addClass("okf-label-culled"); });
      });
    }
    cy.on("position add remove", scheduleLabelCull);

    // Ids within `depth` hops of `root`, over the CURRENTLY-rendered graph
    // (LOD-hidden neighbours are out of scope; documented in the status line).
    function neighborhoodIds(root, depth) {
      var start = cy.getElementById(root);
      if (!start || !start.length) return null;
      var acc = start;
      for (var i = 0; i < depth; i++) acc = acc.closedNeighborhood();
      var ids = {};
      acc.nodes().forEach(function (nn) { ids[nn.id()] = true; });
      return ids;
    }

    // ONE combined filter pass: search + type + focus-neighbourhood +
    // minimum-signal threshold, all expressed as `.dim`. Replaces the old
    // independent handlers that each cleared the others' dim state.
    function applyFilters() {
      var q = controlState.search, ty = controlState.type;
      var thresh = MIN_THRESH[controlState.minLevel] || 0;
      var focusSet = (controlState.focusEnabled && focusRoot) ? neighborhoodIds(focusRoot, controlState.focusDepth) : null;
      var isolationSet = interactionState.isolated ? neighborhoodIds(interactionState.isolated, 1) : null;
      cy.batch(function () {
        cy.nodes().forEach(function (n) {
          var d = n.data(), dim = false;
          if (q) {
            var hay = ((d.label || "") + " " + n.id() + " " + (d.tags || []).join(" ")).toLowerCase();
            dim = hay.indexOf(q) < 0;
            n.toggleClass("okf-search-match", !dim);
            n.toggleClass("okf-search-nonmatch", dim);
          } else {
            n.removeClass("okf-search-match");
            n.removeClass("okf-search-nonmatch");
          }
          if (!dim && ty) dim = d.type !== ty;
          if (!dim && controlState.hiddenTypes[d.type]) dim = true;  // legend chips (phase 4)
          if (!dim && focusSet) dim = !focusSet[n.id()];
          if (!dim && isolationSet) dim = !isolationSet[n.id()];
          n.toggleClass("dim", dim);
        });
        cy.edges().forEach(function (e) {
          var dim = e.source().hasClass("dim") || e.target().hasClass("dim");
          if (!dim && visibleEdgeRelations(e).length===0) dim = true;
          if (!dim && thresh > 0) {
            var keep = controlState.showBridges && e.hasClass("okf-bridge-edge");
            if (!keep && (e.data("weight") || 0) < thresh) dim = true;
          }
          e.toggleClass("dim", dim);
        });
        // Focus root halo: unmistakable marker on the focused node so Focus
        // never looks like Relations (reviewer blocker 4).
        cy.nodes().removeClass("okf-focus-root");
        if (controlState.focusEnabled && focusRoot) {
          var fr = cy.getElementById(focusRoot);
          if (fr && fr.length) fr.addClass("okf-focus-root");
        }
      });
      scheduleLabelCull();
    }

    // ---- P1 Progressive disclosure: edge elision --------------------------
    // Shows/hides edges based on the current view state:
    //   - explicit relationship-detail opt-in → every edge visible
    //   - a node is selected   → its incident edges visible (with .okf-incident)
    //   - a node is hovered    → its incident edges visible (transient)
    //   - a path is traced     → path edges visible
    //   - default              → only the backbone subset
    // Called on init, selection, hover, path, toggle, reset, LOD reveal.
    var hoveredNodeId = null;
    function applyEdgeElision() {
      var tier = typeof densityTier === "string" ? densityTier : "overview";
      if (GRAPH_EMPTY || controlState.relationshipDetailOptIn) {
        cy.edges().removeClass("okf-edge-elided");
        updateDensityIndicator();
        updateStatus();
        return;
      }
      // Automatic zoom never becomes an implicit "show everything" action.
      // Dense intermediate and local-detail views share the deterministic
      // salient tier; overview remains the quieter spanning backbone.
      var bb = computeBackbone(isMobileGraphViewport() ? 0 : (DENSE_GRAPH && tier !== "overview" ? 1 : (DENSE_GRAPH ? 0 : 3)));
      var selId = null;
      var sel = interactionState.selected ? cy.getElementById(interactionState.selected) : cy.nodes(":selected");
      if (sel && sel.length) selId = sel.id();
      var focusNodeId = selId || hoveredNodeId;
      var mobileSelected = isMobileGraphViewport() && !!selId;
      var pathKeys = {};
      if (pathEles) pathEles.edges().forEach(function (e) { pathKeys[e.id()] = true; });
      cy.batch(function () {
        cy.edges().forEach(function (e) {
          var key = e.id();
          var show = (!mobileSelected && !!bb[key])
            || (focusNodeId && (e.source().id() === focusNodeId || e.target().id() === focusNodeId))
            || !!pathKeys[key];
          e.toggleClass("okf-edge-elided", !show);
        });
      });
      updateDensityIndicator();
      updateStatus();
      scheduleLabelCull();
    }

    // Debounced, stale-safe layout rerun. The latest control values always
    // win: a newer schedule STOPS the previous (possibly still-running)
    // layout before starting a fresh one from current state, so a late layout
    // completion can never re-position nodes for a superseded config
    // (async-lifecycle: no stale-result override).
    // ---- Overlay-aware fit (reviewer blockers 1/2/3) -------------------
    // Custom fit that (a) never exceeds MAX_ZOOM, and (b) keeps content out of
    // the rectangle occupied by the Signal panel / node index / legend / LOD
    // pill by insetting the available area per side. When focusing, it fits the
    // selected neighbourhood so Focus visibly isolates it.
    function overlayInsets() {
      var crect = container.getBoundingClientRect();
      var cw = crect.width, ch = crect.height;
      // Review feedback: at high Node spacing / Cluster separation the graph
      // fills the canvas and its outermost nodes + labels can sit right on the
      // viewport edges (reviewer clipping risk). Grow the margin with the spread
      // knobs so the auto-fit frames the extreme spread with breathing room. The
      // extra is 0 at the default (index 1 ⇒ spread ≈ 1.0, so the default look is
      // unchanged) and CAPPED relative to the viewport's short side, so on a
      // narrow phone strip the inset never eats the canvas (mobile stays usable).
      var spc = SPACING[controlState.spacing] != null ? SPACING[controlState.spacing] : 1;
      var sepc = SEPAR[controlState.separation] != null ? SEPAR[controlState.separation] : 1;
      var spread = Math.max(spc, sepc);                          // ~0.55 … ~4.2
      // Keep the breathing room MODEST: too much inset zooms the (already large)
      // max-range graph out far enough to push labels under their LOD floor. ~24
      // px extra clears the corners without shrinking the labels, and the mobile
      // strip is capped tighter so the inset never eats the narrow viewport.
      var extra = Math.max(0, Math.min(24, Math.round(8 * (spread - 1))));
      if (Math.min(cw, ch) < 520) extra = Math.min(extra, 16);
      var base = 22 + extra;                                      // 22 default → ~46 desktop / ~38 mobile
      var ins = { top: base, right: base, bottom: base, left: base };
      function rel(elm) {
        // Settings in the detail pane are not canvas overlays. Including them
        // reserved a phantom top band, shrinking the graph below its bounds.
        if (!elm || !container.contains(elm) || elm.hidden || !elm.getBoundingClientRect) return null;
        var r = elm.getBoundingClientRect();
        if (!r.width || !r.height) return null;
        if (r.right <= crect.left || r.left >= crect.right || r.bottom <= crect.top || r.top >= crect.bottom) return null;
        return { l: r.left - crect.left, r: r.right - crect.left, t: r.top - crect.top, b: r.bottom - crect.top };
      }
      // The Signal bar lives in the detail pane (outside the
      // canvas), so it only insets the fit in the compatibility in-canvas
      // fallback mount.
      var pr = (panelEl && container.contains(panelEl)) ? rel(panelEl) : null;
      if (pr) {
        // Desktop: the panel is a tall LEFT rail — inset the left column. That
        // same inset also clears the bottom-left legend (both live left).
        // Mobile: the panel is a full-width TOP drawer — inset the top instead.
        if ((pr.r - pr.l) > cw * 0.6) ins.top = Math.max(ins.top, pr.b + 14);
        else ins.left = Math.max(ins.left, pr.r + 16);
      }
      // Node index is a small TOP-RIGHT box; push content below its row rather
      // than reserving the whole right column (which squeezed the graph).
      var nr = rel(container.querySelector(".okf-node-index"));
      if (nr) ins.top = Math.max(ins.top, nr.b + 12);
      var pillr = rel(lodPill);
      if (pillr) ins.top = Math.max(ins.top, pillr.b + 10);
      // Density disclosure is plotting chrome too. Its measured footprint
      // participates in fit so high-zoom nodes cannot sit beneath truthful X/Y
      // density copy.
      var densityR = rel(densityIndicator);
      if (densityR) ins.top = Math.max(ins.top, densityR.b + 10);
      var focusR=rel(typeof focusBar!=="undefined"?focusBar:null);if(focusR)ins.top=Math.max(ins.top,focusR.b+12);
      var viewportR=rel(container.querySelector(".okf-graph-zoom"));if(viewportR){if(isMobileGraphViewport()&&cw<=390)ins.bottom=Math.max(ins.bottom,ch-viewportR.t+10);else ins.right=Math.max(ins.right,cw-viewportR.l+10);}
      var minimapR=rel(container.querySelector(".okf-graph-minimap"));if(minimapR)ins.right=Math.max(ins.right,cw-minimapR.l+10);
      // Phase 3: reserve bottom space for the labelled orphan shelf so the
      // fcose-tiled orphans do not underlap the shelf's bottom-edge chrome.
      // The shelf is hidden when the bundle has no orphans (the common
      // case), so this inset is a no-op for orphan-free bundles and only
      // kicks in when the shelf is actually visible (state-ownership:
      // overlayInsets reads live shelf geometry).
      var orphanR = rel(orphanShelf);
      if (orphanR) ins.bottom = Math.max(ins.bottom, (orphanR.b - orphanR.t) + 12);
      // Phase 3 P1: reserve space for the (compact, collapsed-by-default)
      // legend so the auto-fit frames the graph ABOVE it instead of letting
      // nodes underlap the legend toggle. Only the collapsed footprint is
      // reserved — the expanded panel may overlap (explicit user action).
      // Skipped when the orphan shelf lifted the legend to the top.
      if (!container || !container.classList.contains("okf-graph-has-orphans")) {
        var lgd = rel(legend);
        if (lgd) ins.bottom = Math.max(ins.bottom, (lgd.b - lgd.t) + 12);
      } else {
        var lgdTop = rel(legend);
        if (lgdTop) ins.top = Math.max(ins.top, (lgdTop.b - lgdTop.t) + 12);
      }
      // Never let insets consume the whole canvas (small viewports).
      if (ins.left + ins.right > cw - 70) { ins.left = Math.min(ins.left, Math.max(base, cw * 0.45)); ins.right = base; }
      if (ins.top + ins.bottom > ch - 70) { ins.bottom = base; ins.top = Math.min(ins.top, Math.max(base, ch * 0.55)); }
      return ins;
    }
    function focusTargetEles() {
      if (!controlState.focusEnabled || !focusRoot) return null;
      var root = cy.getElementById(focusRoot);
      if (!root || !root.length) return null;
      var nb = root;
      for (var i = 0; i < controlState.focusDepth; i++) nb = nb.closedNeighborhood();
      return nb.nodes().length ? nb : root;
    }
    // Review feedback: layout-agnostic overlap removal. Some layouts (notably
    // the built-in cose on a small near-complete graph) leave nodes piled on
    // top of each other regardless of repulsion/nodeOverlap tuning. This is a
    // cheap force-based separation pass that GUARANTEES a minimum gap between
    // every node pair, for any layout / graph size (capped so the O(n²) loop
    // never runs on very large sets — LOD already caps the canvas at ~100).
    function resolveOverlaps() {
      var list = cy.nodes().toArray();
      if (list.length < 2 || list.length > 260) return;
      // Phase 3 P1: degree-zero nodes are docked into a reserved bottom band
      // by dockOrphanNodes() AFTER this pass, so they are EXCLUDED here. This
      // keeps the main connected graph from being shoved around to make room
      // for nodes that are about to move anyway, and keeps the orphan set
      // out of the O(n^2) collision loop entirely (they get guaranteed
      // spacing from their slot width instead).
      list = list.filter(function (n) {
        return (degreeOf[n.id()] || 0) !== 0 || !shownIds[n.id()];
      });
      if (list.length < 2) return;
      // The guaranteed gap is now driven by the Signal knobs, not a flat
      // 32px. On small/dense bundles plain cose collapses to a pile regardless of
      // idealEdgeLength, so THIS pass — not cose — sets the real spread; keying it
      // off the knobs is what gives Node spacing / Cluster separation visible
      // reach there (the "too bunched up" fix). Each overlap correction only
      // pushes nodes apart, so node collision safety is preserved. The Compact
      // end intentionally trades some of the old flat label-clearance pad for a
      // tighter view; defaults (Balanced spacing + Clear separation) still
      // resolve to ~32px, preserving the prior look.
      // basePad: the minimum gap between ANY two nodes (Node spacing).
      var spc = SPACING[controlState.spacing] != null ? SPACING[controlState.spacing] : 1;
      var sepc = SEPAR[controlState.separation] != null ? SEPAR[controlState.separation] : 1;
      var grouped = controlState.groupBy !== "none";
      var basePad = 2 + 30 * spc;                      // ~18 (Compact) … ~122 (Vast); 32 at Balanced
      // Different-group pairs get a WIDER guaranteed gap (Cluster separation), so
      // groups read as distinct clumps even when cose has piled them together.
      // Floored at basePad so a different group is never closer than a same-group
      // pair; ==basePad at Clear separation, preserving the default.
      var sepMul = Math.max(1, 0.3 + 0.7 * sepc);      // 1.0 at Clear … ~3.2 at Expanse
      var pos = [], grp = [], i, j;
      for (i = 0; i < list.length; i++) {
        var p = list[i].position();
        pos.push({ x: p.x, y: p.y, r: list[i].width() / 2 });
        grp.push(grouped ? groupOf[list[i].id()] : null);
      }
      for (var iter = 0; iter < 60; iter++) {
        var moved = false;
        for (i = 0; i < pos.length; i++) {
          for (j = i + 1; j < pos.length; j++) {
            var dx = pos[j].x - pos[i].x, dy = pos[j].y - pos[i].y;
            var dist = Math.sqrt(dx * dx + dy * dy);
            var pad = (grouped && grp[i] !== grp[j]) ? basePad * sepMul : basePad;
            var minD = pos[i].r + pos[j].r + pad;
            if (dist < minD) {
              if (dist < 0.01) { dx = (i % 2 ? 1 : -1) * (1 + i); dy = (j % 2 ? 1 : -1) * (1 + j); dist = Math.sqrt(dx * dx + dy * dy); }
              var push = (minD - dist) / 2, ux = dx / dist, uy = dy / dist;
              pos[i].x -= ux * push; pos[i].y -= uy * push;
              pos[j].x += ux * push; pos[j].y += uy * push;
              moved = true;
            }
          }
        }
        if (!moved) break;
      }
      cy.batch(function () { for (var k = 0; k < list.length; k++) list[k].position({ x: pos[k].x, y: pos[k].y }); });
    }
    // Separate overlaps, then frame the result. Used after every layout run.
    // Phase 3: label-collision pass. resolveOverlaps() separates the DISCS;
    // this pass separates the full visual footprint — disc PLUS the
    // ellipsized label plate below it — so "Customer SLA Tiers" can no
    // longer sit on top of "Orders" (the reviewer pile-up). Geometry is
    // approximated in model coords from the stylesheet constants (font 13,
    // text-max-width 90 ellipsis → single line ~7px/char, 15px line
    // height); a greedy axis-of-least-overlap push converges in a few
    // iterations on LOD-capped canvases.
    function resolveLabelCollisions() {
      var list = cy.nodes().toArray();
      if (list.length < 2 || list.length > 260) return;
      // Phase 3 P1: docked orphans are excluded — dockOrphanNodes() gives
      // them guaranteed slot spacing, so the label pass only needs to
      // separate the CONNECTED graph's footprints. (Including orphans here
      // would push the main graph off its fcose placement to avoid labels
      // that are about to move to the band.)
      list = list.filter(function (n) {
        return (degreeOf[n.id()] || 0) !== 0 || !shownIds[n.id()];
      });
      if (list.length < 2) return;
      function footprint(n) {
        var p = n.position(), r = (n.width() || 24) / 2;
        var label = String(n.data("label") || "");
        // Ellipsis labels are single-line; width is the lesser of the
        // 90px ceiling and the natural ~7px/char render width.
        var w = Math.max(2 * r, Math.min(90, label.length * 7));
        var h = 15;
        return {
          x1: p.x - w / 2, x2: p.x + w / 2,
          y1: p.y - r, y2: p.y + r + 4 + h,
          n: n,
        };
      }
      for (var iter = 0; iter < 24; iter++) {
        var moved = false;
        var boxes = list.map(footprint);
        for (var i = 0; i < boxes.length; i++) {
          for (var j = i + 1; j < boxes.length; j++) {
            var a = boxes[i], b = boxes[j];
            var ox = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1);
            var oy = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1);
            if (ox <= 0 || oy <= 0) continue;
            var pa = a.n.position(), pb = b.n.position();
            if (ox < oy) {
              var px = ox / 2 + 2;
              var leftFirst = pa.x <= pb.x ? 1 : -1;
              a.n.position({ x: pa.x - px * leftFirst, y: pa.y });
              b.n.position({ x: pb.x + px * leftFirst, y: pb.y });
            } else {
              var py = oy / 2 + 2;
              var topFirst = pa.y <= pb.y ? 1 : -1;
              a.n.position({ x: pa.x, y: pa.y - py * topFirst });
              b.n.position({ x: pb.x, y: pb.y + py * topFirst });
            }
            moved = true;
            boxes[i] = footprint(a.n);
            boxes[j] = footprint(b.n);
          }
        }
        if (!moved) break;
      }
    }

    // Phase 3 P1: deterministic orphan dock. The ACTUAL degree-zero Cytoscape
    // nodes (not only the DOM control surface built by refreshOrphanShelf)
    // are positioned into a reserved bottom band so they read as a labelled
    // "not yet linked" tray instead of floating in the main layout. The set
    // is the same read-only derivation from degreeOf that refreshOrphanShelf
    // uses, sorted by (label, id) for byte-stable ordering. Each orphan is
    // centred in a horizontal slot whose width is >= its full footprint
    // (disc + 90px ellipsis label + margin), so NO two orphan footprints can
    // overlap regardless of count or viewport — desktop AND mobile. The band
    // sits below the connected graph with clearance for the lowest node's
    // label plate. Runs after the overlap/label passes and before
    // overlayAwareFit so the fit frames main graph + dock band together.
    function dockOrphanNodes() {
      var orphans = cy.nodes().toArray().filter(function (n) {
        return !n.hasClass("okf-delta-exit") && !!shownIds[n.id()] && (degreeOf[n.id()] || 0) === 0;
      });
      if (!orphans.length) return;
      orphans.sort(function (a, b) {
        var la = String(a.data("label") || a.id() || "").toLowerCase();
        var lb = String(b.data("label") || b.id() || "").toLowerCase();
        if (la < lb) return -1;
        if (la > lb) return 1;
        var ia = String(a.id()), ib = String(b.id());
        return ia < ib ? -1 : (ia > ib ? 1 : 0);
      });
      var orphanSet = {};
      orphans.forEach(function (n) { orphanSet[n.id()] = true; });
      var mains = cy.nodes().toArray().filter(function (n) {
        return !n.hasClass("okf-delta-exit") && !orphanSet[n.id()];
      });
      var minX = Infinity, maxX = -Infinity, bottomY = -Infinity;
      mains.forEach(function (n) {
        var p = n.position(), r = (n.width() || 24) / 2;
        if (p.x - r < minX) minX = p.x - r;
        if (p.x + r > maxX) maxX = p.x + r;
        if (p.y + r > bottomY) bottomY = p.y + r;
      });
      // Bundle is ALL orphans (no connected nodes): centre a row on the origin.
      if (!isFinite(minX)) { minX = 0; maxX = Math.max(80, orphans.length * 120); bottomY = 0; }
      var maxR = 0;
      orphans.forEach(function (n) { maxR = Math.max(maxR, (n.width() || 24) / 2); });
      // Reserved band below the main graph. +60 clears the lowest connected
      // node's ellipsis label plate (~19px) with margin; +maxR lands the
      // orphan disc CENTRE so its top edge clears the main graph bottom.
      var bandY = bottomY + 60 + maxR;
      // Slot width guarantees footprint separation: disc (2*maxR) + the 90px
      // ellipsis label ceiling + a 6px margin. Adjacent orphan centres are
      // always >= this apart, so neither discs nor the <=90px label plates
      // (each +/-45 from centre) can overlap.
      var slotUnit = 2 * maxR + 96;
      var span = Math.max(maxX - minX, slotUnit * orphans.length);
      var slot = span / orphans.length;
      cy.batch(function () {
        for (var i = 0; i < orphans.length; i++) {
          orphans[i].position({ x: minX + slot * (i + 0.5), y: bandY });
        }
      });
    }

    function readMobileGraphViewport(){return readLiveViewportWidth()<=900;}
    function isMobileGraphViewport(){return initialMobileViewport!==null?initialMobileViewport:readMobileGraphViewport();}
    var mobileLabelWidth=90;
    function compactMobileSmallGraph(){
      var nodes=cy.nodes().filter(function(n){return !n.hasClass("dim");});
      if(!isMobileGraphViewport()||nodes.length<2||nodes.length>12)return;
      // Use the FINAL canvas and live chrome footprint.  The grid deliberately
      // fills roughly 72% of both canvas axes while retaining a 16px boundary;
      // overlayAwareFit then only centres it (the model span already fits at 1x).
      var ins=overlayInsets(),cw=container.clientWidth,ch=container.clientHeight;
      var columns=nodes.length<=4?2:3,rows=Math.ceil(nodes.length/columns);
      // Cytoscape's 18px bounds expansion makes the rendered footprint about
      // 90px. Subtract it before assigning centre spans so the complete union,
      // not merely node centres, occupies ~70% of the canvas.
      // The in-canvas 44px control dock reserves only vertical space. Keep the
      // small-graph grid's horizontal model span full-width; overlayAwareFit's
      // 16px boundary then yields the intended 85–92% readable footprint.
      var compactHeightRatio=1.05-Math.max(0,Math.min(.25,(cw-320)*.25/70));
      var targetW=cw*1.25+18,targetH=Math.min(ch-32,ch*compactHeightRatio);
      var nodeFootprint=90,labelFootprint=90;
      var spanX=Math.max(72,targetW-nodeFootprint),spanY=Math.max(90,targetH-labelFootprint);
      var spacingX=columns>1?spanX/(columns-1):0,spacingY=rows>1?spanY/(rows-1):0;
      mobileLabelWidth=Math.max(52,Math.min(150,spacingX-12));
      cy.style().selector("node").style("text-max-width",mobileLabelWidth).selector(LABEL_PRECEDENCE).style("text-max-width",Math.max(PRECEDENCE_LABEL_WIDTH,mobileLabelWidth)).update();
      refreshLabelBounds();scheduleLabelCull();
      // Degree first puts hubs centrally; alternating row direction shortens
      // common neighbour routes and deterministically reduces grid crossings.
      var ordered=nodes.toArray().sort(function(a,b){var d=(degreeOf[b.id()]||0)-(degreeOf[a.id()]||0);return d||String(a.id()).localeCompare(String(b.id()));});
      cy.batch(function(){ordered.forEach(function(node,index){var row=Math.floor(index/columns),raw=index%columns,count=Math.min(columns,ordered.length-row*columns),slot=count===3?[0,2,1][raw]:raw,col=row%2?count-1-slot:slot;node.position({x:(col-(count-1)/2)*spacingX,y:(row-(rows-1)/2)*spacingY});});});
    }
    // Cytoscape rasterises a label texture under its new style key on the
    // first frame after a label restyle, but sizes it from the label bounds
    // cached under the OLD style until something asks for the bounding box.
    // A window resize across the 900px readable-label switch therefore baked
    // the 25px plate box into the 13px texture (and back), stretching every
    // label until the next restyle. Recompute the bounds synchronously after
    // each label-dimension restyle so the first texture uses the new box.
    // useCache:false matters: the renderer only learns the labels are dirty
    // when it flushes before the next frame, so a cached read here would
    // re-cache the stale dimensions.
    function refreshLabelBounds(){var nodes=cy.nodes();if(nodes.length)nodes.boundingBox({includeLabels:true,useCache:false});}
    var readableStyleKey=null;
    function syncMobileReadableStyle(){var mobile=isMobileGraphViewport(),key=(mobile?"m":"d")+mobileLabelWidth;if(key===readableStyleKey)return;readableStyleKey=key;cy.style().selector("node").style({"font-size":mobile?25:13,"font-weight":mobile?700:600,"line-height":mobile?1.2:1,"min-zoomed-font-size":mobile?10:6,"width":mobile?"mapData(vizSize, 24, 80, 36, 54)":"data(vizSize)","height":mobile?"mapData(vizSize, 24, 80, 36, 54)":"data(vizSize)","text-wrap":"ellipsis","text-max-width":mobile?mobileLabelWidth:90,"text-justification":"center"}).selector(LABEL_PRECEDENCE).style("text-max-width",Math.max(PRECEDENCE_LABEL_WIDTH,mobileLabelWidth)).update();refreshLabelBounds();scheduleLabelCull();}
    function applyInitialSparseSeed(){
      var nodes=cy.nodes().filter(function(n){return !n.hasClass("dim");});
      if(isMobileGraphViewport()||nodes.length<2||nodes.length>12||controlState.preset!=="map"||controlState.lens!=="map")return;
      var columns=Math.min(4,Math.ceil(Math.sqrt(nodes.length))),rows=Math.ceil(nodes.length/columns),cw=container.clientWidth,ch=container.clientHeight;
      var spacingX=Math.max(120,cw*.58/Math.max(1,columns-1)),spacingY=150;
      var ordered=nodes.toArray().sort(function(a,b){return String(a.id()).localeCompare(String(b.id()));});
      cy.batch(function(){ordered.forEach(function(node,index){var row=Math.floor(index/columns),col=index%columns,count=Math.min(columns,ordered.length-row*columns);node.position({x:(col-(count-1)/2)*spacingX,y:(row-(rows-1)/2)*spacingY});});});
    }
    function postLayout() { var mobileSmall=isMobileGraphViewport()&&cy.nodes().length<=12;if(mobileSmall)cy.zoom(1);syncMobileReadableStyle();compactMobileSmallGraph();if(!mobileSmall){resolveOverlaps();resolveLabelCollisions();dockOrphanNodes();}overlayAwareFit(); }

    function overlayAwareFit(eles) {
      if (!cy.nodes().length) return;
      var target = eles || focusTargetEles();
      // Edges (especially bezier control points) may extend far beyond the
      // rendered concepts and used to make sparse graphs look postage-stamp
      // small. Fit the actual node/label geometry: connected component plus
      // the deliberately docked orphan nodes. This is the same geometry in
      // live, static and single-file builds.
      var nodes = (target && target.nodes && target.nodes().length) ? target.nodes() : cy.nodes();
      var bb = nodes.boundingBox({ includeLabels: true, includeOverlays: true });
      if (!bb || !isFinite(bb.w) || !isFinite(bb.h)) return;
       var ins = overlayInsets();if(isMobileGraphViewport())ins={left:Math.max(16,ins.left),right:Math.max(16,ins.right),top:Math.max(16,ins.top),bottom:Math.max(16,ins.bottom)};else if(nodes.length<=12)ins={left:16,right:16,top:16,bottom:16};
      var cw = container.clientWidth, ch = container.clientHeight;
      var availW = Math.max(60, cw - ins.left - ins.right);
      var availH = Math.max(60, ch - ins.top - ins.bottom);
      var safeFit = Math.min(availW / Math.max(bb.w, 1), availH / Math.max(bb.h, 1), MAX_ZOOM);if(isMobileGraphViewport()&&nodes.length<=12)safeFit=Math.min(safeFit,1);
      var z = safeFit;
      if(!isMobileGraphViewport()&&nodes.length<=12){var connectedForWidth=nodes.filter(function(n){return (degreeOf[n.id()]||0)>0;});if(connectedForWidth.length){var connectedWidth=connectedForWidth.boundingBox({includeLabels:true,includeOverlays:true}).w;z=Math.min(z,(cw*.58)/Math.max(connectedWidth,1));}}
      // A sparse connected component should occupy a useful reading width,
      // but only raise the floor when the complete label-aware geometry
      // (including the orphan dock) still fits the unobscured plot.
      if (!target && nodes.length <= 12) {
        var connected = nodes.filter(function (n) { return (degreeOf[n.id()] || 0) > 0; });
        if (connected.length) {
          var connectedBB = connected.boundingBox({ includeLabels: true, includeOverlays: true });
          var readingFloor = (availW * 0.55) / Math.max(connectedBB.w, 1);
          if (readingFloor <= safeFit) z = Math.max(z, readingFloor);
        }
      }
      if (z < 0.06) z = 0.06;
      if(isMobileGraphViewport()&&nodes.length<=12&&bb.h>0){var desiredModelHeight=Math.min(ch*.71,availH)/z,verticalScale=Math.max(1,Math.min(1.3,desiredModelHeight/bb.h)),centerY=bb.y1+bb.h/2;if(verticalScale>1.01){cy.batch(function(){nodes.forEach(function(node){var p=node.position();node.position("y",centerY+(p.y-centerY)*verticalScale);});});bb=nodes.boundingBox({includeLabels:true,includeOverlays:true});}}
      // Phase-4 mobile readability floor: node labels must render at
      // >=13.2 zoomed px on the mobile small-graph grid. The grid models
      // ~72-92% of the canvas span, so when the fit lands a fraction under
      // the floor (canvas aspect shifts — e.g. the compact shell changed
      // the available graph height), raising the fit to the floor keeps
      // the geometry inside the inset boundary. User viewport changes are
      // respected: this only runs inside the programmatic fit.
      if(isMobileGraphViewport()&&nodes.length<=12&&!userViewportInteracted){var labelFloorZoom=13.2/25;if(z<labelFloorZoom)z=labelFloorZoom;}
      cy.zoom(z);
      cy.pan({
        x: ins.left + availW / 2 - z * (bb.x1 + bb.w / 2),
        y: ins.top + availH / 2 - z * (bb.y1 + bb.h / 2)
      });
    }

    // ---- The SINGLE stale-safe layout owner ----------------------------
    // EVERY Cytoscape layout start (debounced controls, layout select, preset,
    // LOD show-all, node-index ensure-visible) goes through runLayoutNow so
    // there is exactly one owner of layout lifecycle. It: (1) BUMPS a monotonic
    // layoutSeq, (2) stops any in-flight layout, and (3) fences the post-layout
    // fit so a completion (layoutstop / timed fallback) from an OLDER run can
    // never re-position or re-fit after a newer one started.
    // layoutStats is exposed via window.__okfLoomGraph for durable async proof.
      var runningLayout = null, layoutTimer = null, layoutDeadlineTimer = null, layoutSeq = 0, lastAppliedSeq = 0, pendingFocusRestore = false, pendingViewportRestore = null, viewportRestoreGeneration = 0;
      var authoritativePayloadNodeCount = Array.isArray(bundle.nodes) ? bundle.nodes.length : 0, initialSeedApplied = false, fitAppliedGeneration = 0;
      var layoutRequired = authoritativePayloadNodeCount > 0;
      var layoutSettled = !layoutRequired;
      var layoutTerminalGeneration = 0, layoutTerminalCode = null, layoutTerminalPhase = null;
    var layoutStats = { starts: 0, applied: 0, skippedStale: 0, lastAppliedSeq: 0 };
      var layoutSuspensionEpoch=0,layoutRestoredEpoch=0,layoutTerminallyHidden=false,captureSuspended=false;
      function abortLayoutForPagehide(event){captureSuspended=true;if(event&&event.persisted){layoutSuspensionEpoch++;layoutTerminallyHidden=false;}else layoutTerminallyHidden=true;++layoutSeq;if(layoutTimer){clearTimeout(layoutTimer);layoutTimer=null;}if(layoutDeadlineTimer){clearTimeout(layoutDeadlineTimer);layoutDeadlineTimer=null;}layoutSettled=false;pendingFocusRestore=false;pendingViewportRestore=null;var abandoned=runningLayout;runningLayout=null;if(abandoned){try{abandoned.stop();}catch(e){}}}
      window.addEventListener("pagehide",abortLayoutForPagehide);
      window.addEventListener("pageshow",function(event){if(!event.persisted||layoutTerminallyHidden||layoutRestoredEpoch===layoutSuspensionEpoch)return;layoutRestoredEpoch=layoutSuspensionEpoch;captureSuspended=false;graphPageActive=true;runLayoutNow(false,false,false);});

    // Apply a layout's settle result exactly ONCE, and only if it is still the
    // current layout. Stale (superseded) completions are counted + dropped.
      function applyLayoutResult(seq) {
        if (seq !== layoutSeq) { layoutStats.skippedStale++; return; }  // superseded → fenced, no fit
        if (seq === layoutTerminalGeneration) { layoutStats.skippedStale++; return; }
        if (seq === lastAppliedSeq) return;                             // one-shot per sequence
        if (layoutDeadlineTimer) { clearTimeout(layoutDeadlineTimer); layoutDeadlineTimer = null; }
      lastAppliedSeq = seq;
      layoutSettled = true;
      layoutStats.applied++;
      layoutStats.lastAppliedSeq = seq;
      postLayout();
      fitAppliedGeneration = seq;
      positionActionBar();
      if(viewportRerunButton){viewportRerunButton.disabled=false;viewportRerunButton.classList.remove("is-running","is-failed");viewportRerunButton.setAttribute("aria-label","Re-run layout");viewportRerunButton.dataset.layoutState="ready";viewportRerunButton.title="Re-run layout";viewportRerunButton.dataset.tooltip="Re-run layout";}
      syncMinimap();
      if(pendingViewportRestore&&pendingViewportRestore.phase==="layout"){var restoreOwner=pendingViewportRestore,restoreViewport=restoreOwner.viewport;if(REDUCED_MOTION){cy.viewport(restoreViewport);if(pendingViewportRestore&&pendingViewportRestore.generation===restoreOwner.generation)pendingViewportRestore=null;}else{restoreOwner.phase="animating";cy.animate({zoom:restoreViewport.zoom,pan:restoreViewport.pan},{duration:200,complete:function(){if(pendingViewportRestore&&pendingViewportRestore.generation===restoreOwner.generation)pendingViewportRestore=null;}});}}
      if(pendingFocusRestore&&!focusRoot){pendingFocusRestore=false;restoreInvokerFocus();}
        // A layout from the previously accepted payload cannot overwrite an
        // in-flight refresh owner's loading marker. The accepted refresh rAF
        // publishes the synchronized terminal state.
        if (!graphRefreshController && fitAppliedGeneration === seq) publishGraphCaptureReady("ready", true);
    }

      function cancelPendingViewportRestore(){viewportRestoreGeneration++;pendingViewportRestore=null;try{cy.stop();}catch(e){}}
      function runLayoutNow(randomize,preserveViewportRestore,explicitTransition) {
      if(pendingViewportRestore&&!preserveViewportRestore)cancelPendingViewportRestore();
      if(layoutTimer){clearTimeout(layoutTimer);layoutTimer=null;}
      // BUMP the owner token BEFORE stopping the previous layout. Cytoscape can
      // emit `layoutstop` SYNCHRONOUSLY inside stop(); bumping first guarantees
      // that stop-triggered completion sees mySeq !== layoutSeq for the OLD run
      // and is fenced. (Previously stop() ran before ++layoutSeq, so a sync
      // stop-triggered fit could pass as current — the async-lifecycle blocker.)
        var mySeq = ++layoutSeq;
        if (layoutDeadlineTimer) { clearTimeout(layoutDeadlineTimer); layoutDeadlineTimer = null; }
        layoutTerminalGeneration = 0; layoutTerminalCode = null; layoutTerminalPhase = null;
      layoutRequired = cy.nodes().length > 0;
      layoutSettled = !layoutRequired;
        if (layoutRequired) publishGraphCaptureReady("loading");
      if (runningLayout) { try { runningLayout.stop(); } catch (e) {} }
      layoutStats.starts++;
      var l = cy.layout(layoutOpts(currentLayoutName,
        Object.assign({ animate: explicitTransition ? "end" : false, animationDuration: 400,
          animationEasing: "ease-in-out", randomize: !!randomize, fit: false }, layoutTuning(controlState))));
      runningLayout = l;
        // `layoutstop` is the ONLY successful settle signal. The deadline never
        // fabricates success: it terminally fails the current owner, stops it,
        // and fences a later layoutstop from changing capture truth.
        var done = function () { applyLayoutResult(mySeq); };
        try { l.on("layoutstop", done); } catch (e) {}
        var testHooks=window.__okfGraphTestHooks,holdInitial=!!(testHooks&&testHooks.holdInitialLayout===true&&mySeq===1);
        if(holdInitial){testHooks.heldGeneration=mySeq;testHooks.emitLateLayoutstop=done;testHooks.releaseInitialLayout=function(){if(mySeq===layoutSeq&&graphPageActive){try{l.run();}catch(e){}}};}
        else try { l.run(); } catch (e) {}
        layoutDeadlineTimer = setTimeout(function () {
          if (mySeq !== layoutSeq || mySeq === lastAppliedSeq) return;
          layoutDeadlineTimer = null;
          layoutTerminalGeneration = mySeq;
          layoutTerminalCode = "unavailable";
          layoutTerminalPhase = "graph-layout-timeout";
          layoutSettled = false;
          if (!graphRefreshController) publishGraphCaptureReady("unavailable", true);
          if(viewportRerunButton){viewportRerunButton.disabled=false;viewportRerunButton.classList.remove("is-running");viewportRerunButton.classList.add("is-failed");viewportRerunButton.setAttribute("aria-label","Re-run layout");viewportRerunButton.dataset.layoutState="failed";viewportRerunButton.title="Layout failed; retry";viewportRerunButton.dataset.tooltip="Layout failed; retry";}
          try { l.stop(); } catch (e) {}
        }, 900);
      }
      function settleEmptyGraph() {
        if (layoutTimer) { clearTimeout(layoutTimer); layoutTimer = null; }
        if (layoutDeadlineTimer) { clearTimeout(layoutDeadlineTimer); layoutDeadlineTimer = null; }
        layoutSeq = 0;
        lastAppliedSeq = 0;
        layoutStats.lastAppliedSeq = 0;
        layoutTerminalGeneration = 0;
        layoutTerminalCode = null;
        layoutTerminalPhase = null;
        layoutRequired = false;
        layoutSettled = true;
        var previous = runningLayout;
        runningLayout = null;
        if (previous) { try { previous.stop(); } catch (e) {} }
        if (!graphRefreshController) publishGraphCaptureReady("ready", true);
      }
    function scheduleLayout(randomize) {
      if (layoutTimer) clearTimeout(layoutTimer);
      layoutTimer = setTimeout(function () { layoutTimer = null; runLayoutNow(randomize, false, true); }, 220);
    }

    function visibleCount(coll) { var c = 0; coll.forEach(function (e) { if (!e.hasClass("dim") && !e.hasClass("okf-edge-elided")) c++; }); return c; }
    function authoritativeVisibleEdgeCount() { return visibleCount(cy.edges()); }
    function relationshipInstanceCount(edges){var count=0;edges.forEach(function(edge){count+=Number(edge.data("multiplicity"))||1;});return count;}
    function visibleRelationshipInstanceCount(){var count=0;cy.edges().forEach(function(edge){if(!edge.hasClass("dim")&&!edge.hasClass("okf-edge-elided"))count+=visibleEdgeRelations(edge).length;});return count;}
    function stampVisibilityProjection(target){if(!target)return;target.dataset.visibleConnectionBundles=String(authoritativeVisibleEdgeCount());target.dataset.totalConnectionBundles=String(cy.edges().length);target.dataset.visibleRelationshipInstances=String(visibleRelationshipInstanceCount());target.dataset.totalRelationshipInstances=String(relationshipInstanceCount(cy.edges()));}
    function statusText() {
      var totN = cy.nodes().length, totE = cy.edges().length;
      if (totN === 0) return "No concepts in this bundle. Find in graph and Graph settings are inactive.";
      var visN = visibleCount(cy.nodes()), visE = authoritativeVisibleEdgeCount();
      var lens = LENSES[controlState.lens] || LENSES.map;
      var s = lens.label + " lens. Showing " + visN + (visN !== totN ? " of " + totN : "") + " concept" + (totN === 1 ? "" : "s");
      var visR=visibleRelationshipInstanceCount(),totR=relationshipInstanceCount(cy.edges());
      s += " and " + visE + (visE !== totE ? " of " + totE : "") + " connection bundle" + (totE === 1 ? "" : "s") + " (" + visR + (visR !== totR ? " of " + totR : "") + " relationship instance" + (totR === 1 ? "" : "s") + ")";
      if (controlState.groupBy !== "none") s += ", grouped by " + GROUP_LABELS[controlState.groupBy];
      s += ", weighted by " + SIGNAL_LABELS[controlState.primarySignal];
      if ((MIN_THRESH[controlState.minLevel] || 0) > 0) s += ". Weak links dimmed";
      if (controlState.showBridges) {
        var bc = 0; Object.keys(bridgeNorm).forEach(function (id) { if (bridgeNorm[id] >= BRIDGE_CUT) bc++; });
        if (bc) s += ". " + bc + " bridge concept" + (bc === 1 ? "" : "s") + " highlighted";
      }
      if (controlState.focusEnabled && focusRoot) {
        var fl = (nodeIndex[focusRoot] && nodeIndex[focusRoot].label) || focusRoot;
        s += ". Exploring neighborhood around " + fl + " (" + controlState.focusDepth + " hop" + (controlState.focusDepth === 1 ? "" : "s") + ")";
      } else if (controlState.focusEnabled && !focusRoot) {
        s += ". Select a concept to explore its neighborhood";
      }
      if (interactionState.selected) {
        var selectedLabel = (nodeIndex[interactionState.selected] && nodeIndex[interactionState.selected].label) || interactionState.selected;
        s += ". Selected: " + selectedLabel;
      }
      if (controlState.search) s += ". " + cy.nodes(".okf-search-match").length + " matches for “" + controlState.search + "”";
      if (interactionState.pathResult) s += ". " + interactionState.pathResult.message;
      s += ".";
      var hidden = 0; bundle.nodes.forEach(function (n) { if (!shownIds[n.data.id]) hidden++; });
      if (hidden > 0) s += " " + hidden + " more available via Show all.";
      return s;
    }
    var statusEl = null, statusTimer = null;
    function updateStatus() {
      if (!statusEl) return;
      // Count data is a synchronous projection of the accepted graph, not an
      // announcement cache. Only spoken text is coalesced: zoom must not leave
      // status on an older tier while the density/list counts already changed.
      stampVisibilityProjection(statusEl);
      if (statusTimer) clearTimeout(statusTimer);
      // Debounced so the aria-live region coalesces rapid slider ticks into
      // one announcement instead of speaking on every step.
      statusTimer = setTimeout(function () { statusTimer = null; statusEl.textContent = statusText();stampVisibilityProjection(statusEl); }, 200);
    }
    function commitVisibleRelationshipProjection(){updateDensityIndicator();if(statusTimer){window.clearTimeout(statusTimer);statusTimer=null;}if(statusEl)statusEl.textContent=statusText();[statusEl,mobileOverviewStatus,densityIndicator,typeof bundleIndex!=="undefined"?bundleIndex:null].forEach(stampVisibilityProjection);}
    function reconcileVisibleRelationshipProjection(){commitVisibleRelationshipProjection();window.requestAnimationFrame(function(){if(!graphPageActive)return;commitVisibleRelationshipProjection();window.requestAnimationFrame(function(){if(graphPageActive)commitVisibleRelationshipProjection();});});}

    // Phase 3 density profile.  Presentation classes only: model counts stay
    // exact.  Overview labels the deterministic high-degree concepts and the
    // connectivity backbone; zooming in restores a deterministic salient tier
    // and full node titles. Complete relationships + labels require Advanced.
    // Derived exclusively from the currently accepted bundle. Live refreshes
    // may cross either threshold, so this is deliberately mutable rather than
    // an initial-payload constant.
    var DENSE_GRAPH = false;
    var DENSITY_THRESHOLDS = [0, 25, 50, 100, 250];
    var DENSITY_LABEL_LIMITS = { 0: Infinity, 25: 12, 50: 16, 100: 20, 250: 28 };
    var densityPolicy = { threshold: 0, labelLimit: Infinity };
    function densityPolicyForCount(nodeCount) {
      var threshold=0;
      DENSITY_THRESHOLDS.forEach(function(value){if(nodeCount>=value)threshold=value;});
      return Object.freeze({threshold:threshold,labelLimit:DENSITY_LABEL_LIMITS[threshold]});
    }
    // Deliberately above the fitted overview zoom: detail is an intentional
    // zoom-in, not something a tall desktop viewport enters automatically.
    var DENSITY_INTERMEDIATE_ZOOM = 1.05;
    var DENSITY_FULL_ZOOM = 1.5;
    var densityTier = "overview";
    var densityIndicator = null;
    var densityPriority = {};
    var brokenConceptCount = 0;
    var BROKEN_AGGREGATION_THRESHOLD = 0.25;
    var aggregateBrokenWarnings = false;
    function deriveDensityState(candidate) {
      var nodeCount = candidate.nodes.length;
      densityPolicy = densityPolicyForCount(nodeCount);
      var threshold = densityPolicy.threshold;
      DENSE_GRAPH = threshold >= 25 || Number(candidate.raw_relationship_count || candidate.edges.length) >= 120;
      document.body.dataset.okfGraphDensityThreshold = String(threshold);
      brokenConceptCount = candidate.nodes.filter(function (n) { return Number(n.data.unresolved_count) > 0; }).length;
      aggregateBrokenWarnings = DENSE_GRAPH && candidate.nodes.length > 0 &&
        brokenConceptCount / candidate.nodes.length > BROKEN_AGGREGATION_THRESHOLD;
      densityPriority = {};
      if (DENSE_GRAPH) candidate.nodes.slice().sort(function (a, b) {
        var delta = (degreeOf[b.data.id] || 0) - (degreeOf[a.data.id] || 0);
        return delta || String(a.data.id).localeCompare(String(b.data.id));
      }).slice(0, densityPolicy.labelLimit).forEach(function (n) {
        densityPriority[n.data.id] = true;
      });
      if (DENSE_GRAPH && !densityIndicator) {
        densityIndicator = document.createElement("div");
        densityIndicator.className = "okf-graph-density";
        densityIndicator.setAttribute("role", "note");
        // Non-plotting chrome is the durable exclusion boundary: pan and zoom
        // can never move a node beneath this disclosure.
        var densityChrome = document.getElementById("okf-detail") || container.parentNode;
        densityChrome.insertBefore(densityIndicator, densityChrome.firstChild);
      } else if (!DENSE_GRAPH && densityIndicator) {
        densityIndicator.remove();
        densityIndicator = null;
      }
    }
    deriveDensityState(bundle);
    var mobileOverview=document.createElement("section"),mobileOverviewStatus=document.createElement("span"),mobileOverviewAll=document.createElement("button");
    mobileOverview.className="okf-mobile-overview";mobileOverview.setAttribute("aria-label","Mobile graph overview");
    mobileOverviewStatus.className="okf-mobile-overview__status";
    mobileOverviewAll.type="button";mobileOverviewAll.className="okf-mobile-overview__all";mobileOverviewAll.textContent="Show all relationships";
    mobileOverviewAll.addEventListener("click",function(){controlState.relationshipDetailOptIn=!controlState.relationshipDetailOptIn;mobileOverviewAll.textContent=controlState.relationshipDetailOptIn?"Show backbone":"Show all relationships";markCustom();cy.edges().removeClass("okf-density-detail-label");applyEdgeLabels();applyEdgeElision();reconcileVisibleRelationshipProjection();});
    mobileOverview.appendChild(mobileOverviewStatus);mobileOverview.appendChild(mobileOverviewAll);if(graphEl&&graphEl.parentNode)graphEl.parentNode.insertBefore(mobileOverview,graphEl.nextSibling);
    if(isMobileGraphViewport())requestAnimationFrame(function(){cy.resize();postLayout();});
    function updateDensityIndicator() {
      if (!cy) return;
      var visibleEdges = authoritativeVisibleEdgeCount();
      [mobileOverviewStatus,densityIndicator].forEach(stampVisibilityProjection);
      mobileOverviewStatus.textContent=visibleEdges+" of "+cy.edges().length+" connection bundles · "+visibleRelationshipInstanceCount()+" of "+relationshipInstanceCount(cy.edges())+" relationship instances · select a concept to inspect its connections";mobileOverviewAll.hidden=false;mobileOverviewAll.textContent=controlState.relationshipDetailOptIn?"Show backbone":"Show all relationships";
      if (!densityIndicator) { updateStatus(); return; }
      var detailInstruction = " · Graph settings → Show relationship labels reveals all relationships and names those of the selected or hovered concept";
      var tierCopy = densityTier === "full"
        ? "Local detail · " + visibleEdges + " of " + cy.edges().length + " relationships (connection bundles) · "+visibleRelationshipInstanceCount()+" relationship instances · node titles remain shortened; use Concepts for full titles" + detailInstruction
        : densityTier === "intermediate"
          ? "Intermediate detail · " + visibleEdges + " of " + cy.edges().length + " relationships (connection bundles) · "+visibleRelationshipInstanceCount()+" relationship instances · labels remain shortened; use Concepts for full titles" + detailInstruction
          : "Overview · " + visibleEdges + " of " + cy.edges().length + " relationships (connection bundles) · "+visibleRelationshipInstanceCount()+" relationship instances · structural backbone only; zoom in for more and use Concepts for full titles" + detailInstruction;
      if (aggregateBrokenWarnings) tierCopy += " · " + brokenConceptCount + " concepts have broken links · review in Concepts";
      densityIndicator.textContent = tierCopy;
      densityIndicator.classList.toggle("okf-graph-density--detail", densityTier !== "overview");
      updateStatus();
    }
    function applyDensityProfile(force) {
      if (!DENSE_GRAPH) {
        cy.batch(function () {
          cy.nodes().removeClass("okf-density-label-hidden okf-broken-aggregated okf-density-overview-broken okf-density-overview-zero");
          cy.edges().removeClass("okf-density-detail-label");
        });
        applyEdgeElision();
        return;
      }
      var zoom = cy.zoom();
      var nextTier = zoom >= DENSITY_FULL_ZOOM ? "full" : (zoom >= DENSITY_INTERMEDIATE_ZOOM ? "intermediate" : "overview");
      if (!force && nextTier === densityTier) return;
      densityTier = nextTier;
      cy.batch(function () {
        cy.nodes().forEach(function (n) {
          var progressiveLabel = !!densityPriority[n.id()] || n.selected() || n.hasClass("okf-hover") ||
            n.hasClass("okf-search-match") || n.hasClass("okf-focus-root") || n.hasClass("okf-path");
          n.toggleClass("okf-density-label-hidden", densityTier !== "full" && !progressiveLabel);
          var overview = densityTier === "overview";
          var broken = Number(n.data("unresolved_count")) > 0;
          var presentZero = n.data("has_active_comment_count") === 1 && n.data("active_comment_count") === 0;
          n.toggleClass("okf-broken-aggregated", aggregateBrokenWarnings && broken);
          n.toggleClass("okf-density-overview-broken", overview && broken && !aggregateBrokenWarnings);
          n.toggleClass("okf-density-overview-zero", overview && !broken && presentZero);
        });
        cy.edges().forEach(function (e) {
          e.removeClass("okf-density-detail-label");
        });
      });
      applyEdgeElision();
      updateDensityIndicator();
    }
    var densityZoomTimer = null;
    cy.on("zoom", function () {
      if (densityZoomTimer) window.clearTimeout(densityZoomTimer);
      densityZoomTimer = window.setTimeout(function () {
        densityZoomTimer = null;
        applyDensityProfile(false);
      }, 40);
    });
    applyDensityProfile(true);

    function updateNodeIndexSwatches() {
      var items = document.querySelectorAll("#okf-node-index-list .okf-node-index__item");
      Array.prototype.forEach.call(items, function (btn) {
        var id = btn.getAttribute("data-target");
        var sw = btn.querySelector(".okf-node-index__swatch");
        if (!sw || !id) return;
        // The swatch mirrors the node's CURRENT canvas fill: neutral by
        // default, the muted lens colour while a colour lens is on.
        var node = cy.getElementById(id);
        sw.style.background = (node && node.length && node.data("color")) || graphPalette().node;
        setForcedCue(sw, node && node.length ? node.data("lensCue") : "");
      });
    }

    // ====================================================================
    // Signal controls — presets, panel build, control wiring
    // ====================================================================
    // ====================================================================
    // Lens analytics — real algorithms behind each lens.
    // --------------------------------------------------------------------
    // Research-grounded (see docs-bundle/explanation/live_studio_design.md,
    // "Why the graph is organised into question-named lenses"): communities are
    // the color dimension that reveals structure (Gephi/InfraNodus/Bloom
    // convergence), PageRank finds what matters better than raw degree,
    // betweenness finds the bridgers (Kumu's "top bridgers"), and
    // timestamps make freshness a first-class lens. All computed locally,
    // lazily, and cached until the graph changes.
    function invalidateLensCache() { _lensCache = {}; }

    // Deterministic label propagation: seeds by sorted id, iterates in
    // sorted order, ties break to the SMALLEST label — same input, same
    // communities, every load. Communities are then renumbered by size
    // (0 = biggest) so colors are stable and legends read biggest-first.
    function computeCommunities() {
      if (_lensCache.communities) return _lensCache.communities;
      // Deterministic single-level-iterated Louvain (modularity
      // optimization) — the Gephi-standard community method. Label
      // propagation was tried first and collapses densely cross-linked
      // bundles into one community; modularity splits them along real
      // link-density boundaries. Determinism: nodes processed in sorted
      // order, ties broken toward the smallest community id, fixed pass
      // cap — same bundle, same communities, every load.
      var ids = Object.keys(nodeIndex).sort();
      var idx = {};
      ids.forEach(function (id, i) { idx[id] = i; });
      var n = ids.length;
      // Undirected weighted adjacency (dedup multi-edges to weight).
      var adj = ids.map(function () { return {}; });
      var m2 = 0;  // 2m (sum of all degrees)
      cy.edges().forEach(function (e) {
        var a = idx[e.source().id()], b = idx[e.target().id()];
        if (a == null || b == null || a === b) return;
        adj[a][b] = (adj[a][b] || 0) + 1;
        adj[b][a] = (adj[b][a] || 0) + 1;
        m2 += 2;
      });
      var community = ids.map(function (_, i) { return i; });
      var degree = adj.map(function (nb) {
        var d = 0; Object.keys(nb).forEach(function (k) { d += nb[k]; }); return d;
      });
      if (m2 > 0) {
        var commTot = degree.slice();       // Σtot per community
        for (var pass = 0; pass < 10; pass++) {
          var moved = false;
          for (var i = 0; i < n; i++) {
            var current = community[i];
            // Links from i into each neighbouring community.
            var kIn = {};
            Object.keys(adj[i]).forEach(function (j) {
              var c = community[j];
              kIn[c] = (kIn[c] || 0) + adj[i][j];
            });
            // Remove i from its community.
            commTot[current] -= degree[i];
            var best = current;
            var bestGain = (kIn[current] || 0) - commTot[current] * degree[i] / m2;
            Object.keys(kIn).map(Number).sort(function (a, b) { return a - b; }).forEach(function (c) {
              if (c === current) return;
              var gain = kIn[c] - commTot[c] * degree[i] / m2;
              if (gain > bestGain + 1e-9) { bestGain = gain; best = c; }
            });
            commTot[best] += degree[i];
            if (best !== current) { community[i] = best; moved = true; }
          }
          if (!moved) break;
        }
      }
      // Renumber by community size (desc), tie → smallest member id.
      var members = {};
      ids.forEach(function (id, i) {
        (members[community[i]] = members[community[i]] || []).push(id);
      });
      var ordered = Object.keys(members).sort(function (a, b) {
        var d = members[b].length - members[a].length;
        return d !== 0 ? d : (members[a][0] < members[b][0] ? -1 : 1);
      });
      var out = {}, list = [];
      ordered.forEach(function (lab, k) {
        members[lab].forEach(function (id) { out[id] = k; });
        list.push({ index: k, ids: members[lab] });
      });
      _lensCache.communities = { of: out, list: list };
      communityOf = out;
      return _lensCache.communities;
    }
      // Themes (communities) take the palette's muted categorical ramp:
      // eight low-chroma hues at about the neutral node's lightness, so the
      // glyph stays >= 4.5:1 and the lens reads as structural grouping, not
      // a second accent. Content types are never colour-coded, so there is
      // no type palette to collide with. Eight stops cycle for >8 themes;
      // the T1, T2… markers carry identity without colour.
      function communityColor(i) {
        var cat = graphPalette().ramp.categorical;
        return cat[((i % cat.length) + cat.length) % cat.length];
      }
      function themeLabel(i) {
        return "T" + (i + 1);
      }

    function computePageRank() {
      if (_lensCache.pagerank) return _lensCache.pagerank;
      var out = {}, max = 0;
      try {
        var pr = cy.elements().pageRank({ dampingFactor: 0.85, precision: 1e-6 });
        cy.nodes().forEach(function (n) {
          var r = pr.rank(n);
          out[n.id()] = r;
          if (r > max) max = r;
        });
        if (max > 0) Object.keys(out).forEach(function (k) { out[k] = out[k] / max; });
      } catch (e) { /* empty graph */ }
      _lensCache.pagerank = out;
      return out;
    }

    function computeBetweenness() {
      if (_lensCache.betweenness) return _lensCache.betweenness;
      var out = {}, max = 0;
      try {
        var bc = cy.elements().betweennessCentrality({ directed: false });
        cy.nodes().forEach(function (n) {
          var b = bc.betweenness(n);
          out[n.id()] = b;
          if (b > max) max = b;
        });
        if (max > 0) Object.keys(out).forEach(function (k) { out[k] = out[k] / max; });
      } catch (e) { /* empty graph */ }
      _lensCache.betweenness = out;
      return out;
    }

    // Recency in days (Infinity = undated). Uses the authored frontmatter
    // timestamp shipped per node.
    function recencyDays(id) {
      var d = nodeIndex[id] || {};
      if (!d.timestamp) return Infinity;
      var t = Date.parse(d.timestamp);
      if (isNaN(t)) return Infinity;
      return Math.max(0, (Date.now() - t) / 86400000);
    }
    function recencyColor(days) {
      // Undated concepts are hollow: the canvas colour inside the 1.5px
      // node stroke ("no data", distinct from every dated step).
      var ramp = graphPalette().ramp;
      if (!isFinite(days)) return ramp.empty;
      // Fresh = the muted accent end of the ordered ramp, fading to the
      // neutral node fill as a concept ages past ~180 days.
      return ramp.sequence(1 - Math.min(1, days / 180));
    }
    function bridgeColor(score) {
      // Neutral for concepts on few paths → the muted accent end of the
      // ordered ramp for the top bridgers.
      return graphPalette().ramp.sequence(Math.max(0, Math.min(1, score)));
    }

    // ====================================================================
    // Lenses — each answers one question a user actually has.
    // Every lens = question + layout + computed encoding + ranked summary.
    // Naming follows the products users understand (Kumu, InfraNodus,
    // yFiles): the structure revealed, never the algorithm.
    // ====================================================================
    var LENSES = {
      // colorMode "type" is the neutral encoding (content types are not
      // colour-coded): Map, Flow and Explore keep neutral round nodes; the
      // Themes, Bridges and Recent lenses colour from the muted ramp.
      map: {
        label: "Map", question: "How does this knowledge fit together?",
        state: { spacing: 1, separation: 1, groupStrength: 1, groupBy: "community",
                 colorMode: "type", sizeMode: "pagerank",
                 focusEnabled: false, relationEdges: false, showEdgeLabels: false,
                 showBridges: false, layout: "cose" },
      },
      themes: {
        label: "Themes", question: "What are the main topic areas — and which barely touch?",
        state: { spacing: 1, separation: 2, groupStrength: 2, groupBy: "community",
                 colorMode: "community", sizeMode: "body",
                 focusEnabled: false, relationEdges: false, showEdgeLabels: false,
                 showBridges: false, layout: "cose" },
      },
      flow: {
        label: "Flow", question: "What feeds into what? (reading left → right)",
        state: { spacing: 1, separation: 1, groupStrength: 0, groupBy: "none",
                 colorMode: "type", sizeMode: "body",
                 focusEnabled: false, relationEdges: true, showEdgeLabels: true,
                 showBridges: false, layout: "dagre" },
      },
      bridges: {
        label: "Bridges", question: "Which concepts hold the areas together?",
        state: { spacing: 2, separation: 2, groupStrength: 1, groupBy: "community",
                 colorMode: "bridge", sizeMode: "betweenness",
                 focusEnabled: false, relationEdges: false, showEdgeLabels: false,
                 showBridges: true, layout: "cose" },
      },
      recent: {
        label: "Recent", question: "What is fresh, what is going stale?",
        state: { spacing: 1, separation: 1, groupStrength: 1, groupBy: "community",
                 colorMode: "recency", sizeMode: "body",
                 focusEnabled: false, relationEdges: false, showEdgeLabels: false,
                 showBridges: false, layout: "cose" },
      },
      focus: {
        label: "Explore neighborhood", question: "What surrounds the selected concept?",
        state: { focusEnabled: true, focusDepth: 1, groupBy: "none",
                 colorMode: "type", sizeMode: "body",
                 relationEdges: false, showEdgeLabels: true, showBridges: false },
      },
    };
    var ui = {};

    function projectLens(name, focusCanonical) {
      if(name==="focus"&&!focusCanonical&&interactionState.selected){enterFocus(interactionState.selected,"lens");return;}
      var lens = LENSES[name];
      if (!lens) return;
      var p = lens.state;
      Object.keys(p).forEach(function (k) { controlState[k] = p[k]; });
      controlState.preset = name;
      controlState.lens = name;
      if (p.layout) {
        currentLayoutName = p.layout;
        controlState.layout = p.layout;
      }
      if (name === "focus" && !focusRoot) {
        var selNode = cy.nodes(":selected");
        if (selNode && selNode.length) focusRoot = selNode.id();
      }
      // Compute what the lens needs BEFORE encoding paints it.
      if (p.colorMode === "community" || p.groupBy === "community") computeCommunities();
      if (p.sizeMode === "pagerank") computePageRank();
      if (p.sizeMode === "betweenness" || p.colorMode === "bridge" || p.showBridges) computeBetweenness();
      syncControlsFromState();
      applyGrouping();
      applyVisualEncoding();
      applyFilters();
      updateFocusControls();
                updateStatus();
                renderLensSummary();
                refreshLodPill();
      // randomize:false: the layout REFINES current positions, so lens
      // switches read as reprojections of the same map, not new worlds.
      scheduleLayout(false);
    }
    function applyLens(name, focusCanonical) {
      return dispatchInteraction({type:"LENS",name:name,focusCanonical:!!focusCanonical,quietUrl:true});
    }


    // ---- Lens summary: the ranked answers beside the picture -----------
    // Every analytic view ships a clickable ranked list (the pattern that
    // makes Kumu/InfraNodus/Graph-Analysis land where bare pictures
    // don't). Rendered into #detail-empty, i.e. visible whenever no node
    // is selected; click a row -> select that node.
    function renderLensSummary() {
      var empty = document.getElementById("detail-empty");
      if (!empty) return;
      while (empty.firstChild) empty.removeChild(empty.firstChild);
      empty.classList.add("okf-lens-summary");
      var lensKey = controlState.lens;
      var ids = Object.keys(nodeIndex);
      if (!ids.length) { empty.appendChild(el("p", {}, ["No concepts in this bundle."])); return; }
      var frag = document.createDocumentFragment();
      function heading(text) { frag.appendChild(el("h2", { class: "okf-lens-summary__title" }, [text])); }
      function verdict(text) { frag.appendChild(el("p", { class: "okf-lens-summary__verdict" }, [text])); }
      // A ranked row: hairline record, label, mono figure, and a 2px score
      // bar along the foot (never a filled bar). The row a reader selected
      // is current (aria-current + the 2px accent edge in graph.css).
      function row(id, note, score) {
        var d = nodeIndex[id] || {};
        var btn = el("button", { type: "button", class: "okf-lens-summary__row", "data-target": id,
          "aria-current": selectedConceptId && selectedConceptId === id ? "true" : null });
        var bar = el("span", { class: "okf-lens-summary__bar", "aria-hidden": "true" });
        bar.style.width = Math.round(Math.max(0.04, Math.min(1, score)) * 100) + "%";
        btn.appendChild(bar);
        btn.appendChild(el("span", { class: "okf-lens-summary__label" }, [d.label || id]));
        btn.appendChild(el("span", { class: "okf-lens-summary__note" }, [note]));
        btn.addEventListener("click", function () { clearPath(); dispatchInteraction({type:"SELECT",id:id,source:"summary"}); });
        frag.appendChild(btn);
      }
      var orphans = ids.filter(function (id) {
        return ((outgoing[id] || []).length + (backlinks[id] || []).length) === 0;
      });

      if (lensKey === "bridges") {
        var bt = computeBetweenness();
        heading("Top bridgers");
        verdict("These concepts sit on the paths between themes; losing one disconnects areas.");
        ids.slice().sort(function (a, b) { return (bt[b] || 0) - (bt[a] || 0); })
          .slice(0, 8).forEach(function (id) {
            row(id, Math.round((bt[id] || 0) * 100) + "%", bt[id] || 0);
          });
      } else if (lensKey === "recent") {
        heading("Freshness");
        var dated = ids.filter(function (id) { return isFinite(recencyDays(id)); });
        var stale = dated.filter(function (id) { return recencyDays(id) > 180; });
        var undated = ids.length - dated.length;
        verdict(stale.length + " of " + ids.length + " concepts are older than ~6 months" +
          (undated ? "; " + undated + " carry no timestamp" : "") + ".");
        dated.sort(function (a, b) { return recencyDays(b) - recencyDays(a); })
          .slice(0, 8).forEach(function (id) {
            var d = recencyDays(id);
            row(id, d > 365 ? Math.round(d / 365) + "y old" : Math.round(d) + "d old",
                Math.min(1, d / 365));
          });
      } else if (lensKey === "flow") {
        heading("Sources and sinks");
        var roots = ids.filter(function (id) { return (backlinks[id] || []).length === 0 && (outgoing[id] || []).length > 0; });
        var leaves = ids.filter(function (id) { return (outgoing[id] || []).length === 0 && (backlinks[id] || []).length > 0; });
        verdict(roots.length + " sources feed the flow; " + leaves.length + " sinks end it. Arrows read left to right.");
        var maxOut = 1;
        ids.forEach(function (id) { maxOut = Math.max(maxOut, (outgoing[id] || []).length); });
        ids.slice().sort(function (a, b) { return (outgoing[b] || []).length - (outgoing[a] || []).length; })
          .slice(0, 8).forEach(function (id) {
            var n = (outgoing[id] || []).length;
            row(id, "feeds " + n, n / maxOut);
          });
      } else if (lensKey === "focus") {
        heading("Explore neighborhood");
        verdict(focusRoot
          ? "Showing the " + controlState.focusDepth + "-hop neighborhood of the selected concept. Depth is in Graph settings."
          : "Select any concept to explore its neighborhood. Everything else fades but stays in place.");
      } else {
        // map / themes: the community digest.
        var com = computeCommunities();
        var pr = computePageRank();
        heading("Suggested starting points");
        ids.slice().sort(function (a, b) { return (pr[b] || 0) - (pr[a] || 0) || a.localeCompare(b); })
          .slice(0, Math.min(5, ids.length)).forEach(function (id) {
            row(id, (degreeOf[id] || 0) + " connections", pr[id] || 0);
          });
        var primarySummary = frag;
        if (lensKey !== "themes") {
          frag = el("details", { class: "okf-graph-theme-digest" });
          frag.appendChild(el("summary", {}, ["Themes in this graph"]));
        }
        heading(lensKey === "themes" ? "Themes" : "Themes in this graph");
        var top = com.list.slice(0, 6).filter(function (c) { return c.ids.length > 1; });
        var linked = {};
        cy.edges().forEach(function (e) {
          var a = communityOf[e.source().id()], b = communityOf[e.target().id()];
          if (a != null && b != null && a !== b) linked[Math.min(a, b) + ":" + Math.max(a, b)] = true;
        });
        var gap = null;
        for (var i = 0; i < top.length && !gap; i++) {
          for (var j = i + 1; j < top.length && !gap; j++) {
            if (!linked[top[i].index + ":" + top[j].index]) gap = [top[i], top[j]];
          }
        }
        function exemplar(c) {
          return c.ids.slice().sort(function (a, b) { return (pr[b] || 0) - (pr[a] || 0); })[0];
        }
        var themes = com.list.filter(function (c) { return c.ids.length > 1; }).length;
        var bits = themes + " theme" + (themes === 1 ? "" : "s");
        if (orphans.length) bits += " - " + orphans.length + " not yet linked";
        if (gap) {
          var ga = nodeIndex[exemplar(gap[0])] || {}, gb = nodeIndex[exemplar(gap[1])] || {};
          bits += " - the areas around \u201c" + (ga.label || "?") + "\u201d and \u201c" + (gb.label || "?") + "\u201d share no links";
        }
        verdict(bits + ".");
        com.list.slice(0, 8).forEach(function (c) {
          if (c.ids.length < 2 && com.list.length > 3) return;
          var ex = exemplar(c);
          var d = nodeIndex[ex] || {};
          var btn = el("button", { type: "button", class: "okf-lens-summary__row okf-lens-summary__row--theme", "data-target": ex,
            "aria-current": selectedConceptId && selectedConceptId === ex ? "true" : null });
          // The theme's 2px bar takes its canvas colour while themes colour
          // the canvas (syncLensSummaryColours keeps legend and canvas in
          // step); otherwise it is the neutral rule.
          var bar = el("span", { class: "okf-lens-summary__bar", "aria-hidden": "true", "data-community": String(c.index) });
          bar.style.width = Math.round(100 * c.ids.length / Math.max(1, com.list[0].ids.length)) + "%";
          btn.appendChild(bar);
            btn.appendChild(el("span", { class: "okf-lens-summary__label" },
              [themeLabel(c.index) + " \u00b7 around \u201c" + (d.label || ex) + "\u201d"]));
          btn.appendChild(el("span", { class: "okf-lens-summary__note" }, [c.ids.length + " concepts"]));
          btn.addEventListener("click", function () { clearPath(); dispatchInteraction({type:"SELECT",id:ex,source:"summary"}); });
          frag.appendChild(btn);
        });
        if (frag !== primarySummary) { primarySummary.appendChild(frag); frag = primarySummary; }
      }
      frag.appendChild(el("p", { class: "okf-lens-summary__hint okf-muted" },
        ["Choose a row to select it on the canvas. Tap or hover concepts to preview connections; keyboard users can press Shift+Space at two concepts to trace a path."]));
      empty.appendChild(frag);
      syncLensSummaryColours();
    }

    function markCustom() {
      controlState.preset = "custom";
      (ui.presets || []).forEach(function (r) { r.checked = false; });
    }

    function setRange(input, value, labels) {
      input.value = value;
      input.setAttribute("aria-valuetext", labels[value]);
      if (input.__valEl) input.__valEl.textContent = labels[value];
    }
    function syncControlsFromState() {
      if (!ui.spacing) return;
      setRange(ui.spacing, controlState.spacing, SPACE_LABELS);
      setRange(ui.separation, controlState.separation, SEP_LABELS);
      setRange(ui.groupStrength, controlState.groupStrength, STR_LABELS);
        if (ui.relLabels) ui.relLabels.checked = controlState.showEdgeLabels;
        if (ui.themeColors) ui.themeColors.checked = controlState.colorMode === "community";
      if (ui.groupBy) ui.groupBy.value = controlState.groupBy;
      if (ui.groupColors) ui.groupColors.checked = controlState.colorMode === "type" && controlState.colorBy === "group";
      if (ui.depth) ui.depth.value = String(controlState.focusDepth);
      (ui.presets || []).forEach(function (r) { r.checked = (r.value === controlState.lens); });
      if (ui.question) {
        var lens = LENSES[controlState.lens];
        ui.question.textContent = lens ? lens.question : "";
      }
    }

    function updateFocusControls() {
      if (!ui.depth) return;
      var active = controlState.focusEnabled;
      ui.depth.disabled = !active;
      // Do not expose an unavailable action fragment. Clear focus is a real,
      // fully operable action only while a focus root owns the view.
      if (ui.clearFocus) ui.clearFocus.hidden = !(active && focusRoot);
      if (ui.focusHint) {
        if (active && !focusRoot) { ui.focusHint.hidden = false; }
        else { ui.focusHint.hidden = true; }
      }
    }

    function rangeRow(parent, labelText, max, value, labels, onChange) {
      var valEl = el("span", { class: "okf-signal__row-value" }, [labels[value]]);
      var head = el("span", { class: "okf-signal__row-label" }, [labelText + ": ", valEl]);
      var input = el("input", { type: "range", min: "0", max: String(max), step: "1", value: String(value), "aria-label": labelText });
      input.setAttribute("aria-valuetext", labels[value]);
      input.__valEl = valEl;
      input.addEventListener("input", function () {
        var v = parseInt(input.value, 10) || 0;
        valEl.textContent = labels[v];
        input.setAttribute("aria-valuetext", labels[v]);
        onChange(v);
      });
      parent.appendChild(el("label", { class: "okf-signal__row okf-signal__row--range" }, [head, input]));
      return input;
    }
    function selectRow(parent, labelText, options, value, onChange) {
      var sel = el("select", { "aria-label": labelText });
      options.forEach(function (o) {
        var opt = el("option", { value: o[0] }, [o[1]]);
        if (o[2] === false) opt.disabled = true;
        sel.appendChild(opt);
      });
      sel.value = value;
      sel.addEventListener("change", function () { onChange(sel.value); });
      parent.appendChild(el("label", { class: "okf-signal__row" }, [el("span", { class: "okf-signal__row-label" }, [labelText]), sel]));
      return sel;
    }
    function checkRow(parent, labelText, checked, onChange) {
      var input = el("input", { type: "checkbox" });
      input.checked = checked;
      input.addEventListener("change", function () { onChange(input.checked); });
      parent.appendChild(el("label", { class: "okf-signal__row okf-signal__row--check" }, [input, el("span", {}, [labelText])]));
      return input;
    }

    function lensPreview(key) {
      var svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 60 40");svg.setAttribute("width","60");svg.setAttribute("height","40");svg.setAttribute("aria-hidden","true");svg.classList.add("okf-lens-preview","okf-lens-preview--"+key);
      var layouts={map:[[9,20],[25,8],[30,29],[50,15]],themes:[[10,12],[18,18],[43,10],[50,19]],flow:[[7,20],[25,10],[25,30],[52,20]],bridges:[[8,10],[8,30],[30,20],[52,10],[52,30]],recent:[[9,20],[24,20],[39,20],[53,20]],focus:[[8,8],[15,28],[30,20],[48,10],[51,30]]},pts=layouts[key]||layouts.map;
      var edges=key==="flow"?[[0,1],[0,2],[1,3],[2,3]]:key==="bridges"?[[0,2],[1,2],[2,3],[2,4]]:[[0,1],[1,2],[2,3],[0,2]];
      edges.forEach(function(pair){var line=document.createElementNS(svg.namespaceURI,"line");line.setAttribute("x1",pts[pair[0]][0]);line.setAttribute("y1",pts[pair[0]][1]);line.setAttribute("x2",pts[pair[1]][0]);line.setAttribute("y2",pts[pair[1]][1]);svg.appendChild(line);});
      pts.forEach(function(p,i){var c=document.createElementNS(svg.namespaceURI,"circle");c.setAttribute("cx",p[0]);c.setAttribute("cy",p[1]);c.setAttribute("r",key==="focus"&&i===2?"5":key==="bridges"&&i===2?"4.5":"3");c.setAttribute("data-rank",String(i));if(key==="recent")c.setAttribute("opacity",String(.3+i*.2));svg.appendChild(c);});
      if(key==="flow")[[25,8],[25,28],[52,18]].forEach(function(p){var a=document.createElementNS(svg.namespaceURI,"path");a.setAttribute("d","M"+(p[0]-3)+" "+(p[1]-2)+" L"+p[0]+" "+p[1]+" L"+(p[0]-3)+" "+(p[1]+2));a.setAttribute("class","okf-preview-arrow");svg.appendChild(a);});
      if(key==="themes")[[5,5,20,20],[36,4,20,21]].forEach(function(r){var hull=document.createElementNS(svg.namespaceURI,"rect");hull.setAttribute("x",r[0]);hull.setAttribute("y",r[1]);hull.setAttribute("width",r[2]);hull.setAttribute("height",r[3]);hull.setAttribute("rx","0");hull.setAttribute("class","okf-preview-hull");svg.insertBefore(hull,svg.firstChild);});
      if(key==="focus"){var ring=document.createElementNS(svg.namespaceURI,"circle");ring.setAttribute("cx","30");ring.setAttribute("cy","20");ring.setAttribute("r","13");ring.setAttribute("class","okf-preview-ring");svg.insertBefore(ring,svg.firstChild);}
      return svg;
    }
    var lastRelationshipFilterFocus=null;
    function buildFilterChips(parent, values, hiddenState, kind) {
      var wrap=el("div",{class:"okf-filter-chips","aria-label":kind+" filters"});
      function refresh(){Array.prototype.forEach.call(wrap.querySelectorAll("[data-filter-value]"),function(button){var off=!!hiddenState[button.dataset.filterValue],count=kind==="Relationship"?relationValueCount(button.dataset.filterValue):bundle.nodes.filter(function(n){return n.data.type===button.dataset.filterValue;}).length;button.setAttribute("aria-pressed",String(!off));button.setAttribute("aria-label",button.dataset.filterValue+", "+count+" "+(kind==="Relationship"?"relationship":"concept")+(count===1?"":"s")+", "+(off?"hidden":"shown"));button.classList.toggle("is-off",off);var text=button.querySelector(".okf-filter-chip__text");if(text)text.textContent=button.dataset.filterValue+" ("+count+")";});}
      function commitFilterVisibility(){if(kind==="Relationship")reconcileVisibleRelationshipProjection();else updateStatus();}
      values.forEach(function(value){var group=el("span",{class:"okf-filter-chip-group"});var toggle=el("button",{type:"button",class:"okf-filter-chip","data-filter-value":value},[el("span",{class:"okf-filter-chip__text"},[value])]);if(kind==="Relationship")toggle.addEventListener("focus",function(){lastRelationshipFilterFocus={value:value,solo:false};document.body.dataset.okfRelationshipFilterFocus=value;document.body.dataset.okfRelationshipFilterFocusSolo="0";});toggle.addEventListener("click",function(e){var candidate=Object.assign({},hiddenState);if(e.altKey){values.forEach(function(v){candidate[v]=v!==value;});}else candidate[value]=!candidate[value];if(!commitHiddenFilterState(kind,hiddenState,candidate)){refresh();return;}renderInteraction();refresh();commitFilterVisibility();writeInteractionUrl("replace");syncMinimap();});var solo=el("button",{type:"button",class:"okf-filter-solo","data-filter-solo-value":value,"aria-label":"Show only "+value},["Only"]);if(kind==="Relationship")solo.addEventListener("focus",function(){lastRelationshipFilterFocus={value:value,solo:true};document.body.dataset.okfRelationshipFilterFocus=value;document.body.dataset.okfRelationshipFilterFocusSolo="1";});solo.addEventListener("click",function(){var candidate={};values.forEach(function(v){candidate[v]=v!==value;});if(!commitHiddenFilterState(kind,hiddenState,candidate)){refresh();return;}renderInteraction();refresh();commitFilterVisibility();writeInteractionUrl("replace");syncMinimap();});group.appendChild(toggle);group.appendChild(solo);wrap.appendChild(group);});
      var all=el("button",{type:"button",class:"okf-filter-showall"},["Show all"]);all.addEventListener("click",function(){values.forEach(function(v){delete hiddenState[v];});renderInteraction();refresh();commitFilterVisibility();writeInteractionUrl("replace");syncMinimap();});wrap.appendChild(all);refresh();parent.appendChild(wrap);return wrap;
    }
    function refreshRelationFilterControls(){var values=allRelationValues(),valid={},old=document.querySelector('.okf-filter-chips[aria-label="Relationship filters"]'),active=document.activeElement,focusSolo=!!(active&&active.classList&&active.classList.contains("okf-filter-solo")),focusValue=active&&old&&old.contains(active)?(focusSolo?active.dataset.filterSoloValue:active.dataset.filterValue):null;values.forEach(function(value){valid[value]=true;});Object.keys(controlState.hiddenRelations).forEach(function(value){if(!valid[value])delete controlState.hiddenRelations[value];});if(!filterStateHasVisibleInstance("Relationship",controlState.hiddenRelations))replaceRecord(controlState.hiddenRelations,{});if(!old){updateLegend();return;}var holder=document.createElement("div");buildFilterChips(holder,values,controlState.hiddenRelations,"Relationship");old.replaceWith(holder.firstChild);if(focusValue){var group=holder.firstChild.querySelector('[data-filter-value="'+CSS.escape(focusValue)+'"]');var target=focusSolo&&group?group.parentElement.querySelector('.okf-filter-solo'):group;if(target)target.focus();}}

    function buildSignalPanel() {
      // Lens explanation and expert controls mount into a template-owned slot
      // after the selected concept detail. This keeps the authored hierarchy
      // stable across live, static, and single-file viewers.
      var panel = el("section", { class: "okf-signal okf-signal--bar", role:"region", "aria-labelledby":"okf-lenses-heading" });
      panel.appendChild(el("h2",{id:"okf-lenses-heading",class:"sr-only"},["Lenses and graph key"]));

      // Lenses (native radio group) — always visible. Each chip carries
      // its question as the tooltip; the active question paints under the
      // bar so switching a lens always tells you what you are looking at.
      var lenses = el("details", { class: "okf-signal__lenses" });
      lenses.appendChild(el("summary", {}, ["Explore with lenses"]));
      var seg = el("div", { class: "okf-signal__seg", role: "radiogroup", "aria-label": "Lens" });
      ui.presets = [];
      Object.keys(LENSES).forEach(function (key) {
        var lens = LENSES[key];
        var rid = "okf-lens-" + key;
        var input = el("input", { type: "radio", name: "okf-lens", id: rid, value: key });
        input.checked = controlState.lens === key;
        input.addEventListener("change", function () { if (input.checked) { cancelPendingViewportRestore();if(key==="focus")activateFocusLens("lens");else{applyLens(key);writeInteractionUrl("push");} } });
        ui.presets.push(input);
        input.setAttribute("aria-label",lens.label+": "+lens.question);
        var visual=el("span",{class:"okf-lens-choice"});visual.appendChild(lensPreview(key));visual.appendChild(el("b",{},[lens.label]));visual.appendChild(el("small",{},[lens.question]));
        var item = el("label", { class: "okf-signal__seg-item", "for": rid, title: lens.question },[input,visual]);
        seg.appendChild(item);
      });
      lenses.appendChild(seg);
      // Clear focus only exists visually while Focus is active; an inert or
      // clipped fragment must never appear in the normal Details stack.
      ui.clearFocus = el("button", { type: "button", class: "okf-signal__btn", hidden: "hidden" }, ["Clear neighborhood"]);
      ui.clearFocus.addEventListener("click", function () {
        exitFocus();
      });
      lenses.appendChild(ui.clearFocus);
      panel.appendChild(lenses);

      // Advanced disclosure — the expert knobs, collapsed by default.
      var adv = el("details", { class: "okf-signal__advanced" });
      adv.appendChild(el("summary", { class: "okf-signal__summary" }, ["Graph settings"]));
      var body = el("div", { class: "okf-signal__body" });
      ui.settingsBody = body;
      adv.appendChild(body);
      panel.appendChild(adv);
      body.appendChild(legend);
      body.appendChild(el("p", { class: "okf-signal__hint okf-muted" },
        ["Lenses own the layout and colours. These knobs fine-tune the active lens."]));

      // View: the type filter (relocated from the topbar) + relationship
      // label toggle. The layout dropdown is GONE — each lens owns its
      // layout (evidence: professional tools ship 3 semantic layouts, not
      // an algorithm buffet).
      var vfs = el("fieldset", { class: "okf-signal__group" });
      vfs.appendChild(el("legend", {}, ["Filters"]));
       if(typeSel){typeSel.hidden=true;typeSel.setAttribute("aria-hidden","true");typeSel.tabIndex=-1;var legacyTypeLabel=typeSel.closest("label");if(legacyTypeLabel)legacyTypeLabel.hidden=true;}
      var groupOpts = [["community", "Theme"], ["type", "Type"], ["tag", "First tag"], ["relation", "Relation type"], ["neighborhood", "Connected component"]];
      if (HAS_FOLDERS) groupOpts.push(["folder", "Folder"]);
      if (HAS_GRAPH_CLUSTER) groupOpts.push(["graph_cluster", "Graph cluster"]);
      if (HAS_SOURCE_SYSTEM) groupOpts.push(["source_system", "Source system"]);
      if (HAS_PROJECT) groupOpts.push(["project", "Project"]);
      if (HAS_SECTION) groupOpts.push(["section", "Section"]);
      if (HAS_IMPORT_BATCH) groupOpts.push(["import_batch", "Import batch"]);
      if (HAS_REDMINE_PROJECT) groupOpts.push(["redmine_project", "Redmine project"]);
      groupOpts.push(["none", "None"]);
      ui.groupBy = selectRow(vfs, "Group by", groupOpts, controlState.groupBy, function (v) {
        controlState.groupBy = v;
        if (v === "community") computeCommunities();
        markCustom(); applyGrouping(); applyVisualEncoding(); applyFilters(); updateLegend(); updateStatus(); scheduleLayout(false);
      });
        ui.relLabels = checkRow(vfs, "Show relationship labels", controlState.showEdgeLabels, function (c) {
          // Labels and the complete relationship set only: hue stays the
          // Relations lens's encoding, so the map keeps its muted palette.
          controlState.showEdgeLabels = c;
          controlState.relationshipDetailOptIn = c;
           markCustom(); applyVisualEncoding(); applyEdgeLabels(); applyEdgeElision(); reconcileVisibleRelationshipProjection();
          // Cytoscape may defer style/class projection until the current event
          // returns. Re-read the authoritative post-toggle collection on the
          // next frame so disclosure, mobile overview and aria-live status
          // cannot retain the pre-toggle subset count.
        });
      // Saket et al. 2014: group colouring aids cluster tasks but costs
      // ~25% accuracy on plain topology tasks — so the evidence says make
      // it a toggle. Off = the neutral node fill (types are never coloured);
      // on = the palette's muted categorical ramp.
      ui.themeColors = checkRow(vfs, "Colour by theme", controlState.colorMode === "community", function (c) {
        controlState.colorMode = c ? "community" : "type";
        if (c) controlState.colorBy = "type";
        if (ui.groupColors) ui.groupColors.checked = false;
        applyGrouping();
        applyVisualEncoding(); updateLegend(); updateStatus();
      });
      ui.groupColors = checkRow(vfs, "Colour by selected group", controlState.colorMode === "type" && controlState.colorBy === "group", function (c) {
        controlState.colorMode = "type";
        controlState.colorBy = c ? "group" : "type";
        if (ui.themeColors) ui.themeColors.checked = false;
        markCustom(); applyGrouping(); applyVisualEncoding(); updateLegend(); updateStatus();
      });
      body.appendChild(vfs);

      // Spacing.
      var sfs = el("fieldset", { class: "okf-signal__group" });
      sfs.appendChild(el("legend", {}, ["Spacing"]));
      ui.spacing = rangeRow(sfs, "Node spacing", SPACE_LABELS.length - 1, controlState.spacing, SPACE_LABELS, function (v) { controlState.spacing = v; markCustom(); applyEdgeLabels(); scheduleLayout(false); updateStatus(); });
      ui.separation = rangeRow(sfs, "Cluster separation", SEP_LABELS.length - 1, controlState.separation, SEP_LABELS, function (v) { controlState.separation = v; markCustom(); scheduleLayout(false); updateStatus(); });
      ui.groupStrength = rangeRow(sfs, "Group strength", STR_LABELS.length - 1, controlState.groupStrength, STR_LABELS, function (v) { controlState.groupStrength = v; markCustom(); scheduleLayout(false); updateStatus(); });
      body.appendChild(sfs);

      // Neighborhood exploration.
      var ffs = el("fieldset", { class: "okf-signal__group" });
      ffs.appendChild(el("legend", {}, ["Explore neighborhood"]));
      ui.depth = selectRow(ffs, "Depth", [["1", "1 hop"], ["2", "2 hops"]], String(controlState.focusDepth), function (v) {
        controlState.focusDepth = parseInt(v, 10) || 1;if(focusDepth)focusDepth.value=String(controlState.focusDepth);applyFilters();updateStatus();runLayoutNow(true,false,true);renderInteraction();writeInteractionUrl("replace");
      });
      ui.focusHint = el("p", { class: "okf-signal__row-hint okf-muted", hidden: "hidden" }, ["Select a concept to explore its neighborhood."]);
      ffs.appendChild(ui.focusHint);
      body.appendChild(ffs);

      body.appendChild(el("p", { class: "okf-signal__edgekey okf-muted" },
        ["Thicker links carry more connective weight. Hover any edge to read its relationship."]));

      // Question + status follow the controls and remain the single live-region
      // owner for graph state changes.
      ui.question = el("p", { class: "okf-signal__question" });
      panel.appendChild(ui.question);
      statusEl = document.getElementById("okf-graph-announcer") || el("p", { id:"okf-graph-announcer", class: "okf-signal__status", role: "status", "aria-live": "polite", "aria-atomic":"true" });
      panel.appendChild(statusEl);

      panelEl = panel;
      var detailEl = document.getElementById("okf-detail");
      var lensSlot = document.getElementById("okf-lens-controls-slot");
      if (detailEl && lensSlot) {
        if (MODE === "static" || IS_SINGLE) {
          var notice = el("p", { class: "okf-detail-view-only", role: "note" },
            ["View only; graph exploration remains interactive."]);
          lensSlot.appendChild(notice);
        }
        lensSlot.appendChild(panel);
      } else {
        // In-canvas fallback: stop pointer events bubbling into Cytoscape
        // (background-tap would clear the selection on every slider drag).
        ["mousedown", "touchstart", "pointerdown", "click", "dblclick", "wheel"].forEach(function (evName) {
          panel.addEventListener(evName, function (e) { e.stopPropagation(); });
        });
        container.insertBefore(panel, container.firstChild);
      }
    }

    // Empty-graph hardening (reviewer #9): with zero nodes there is nothing to
    // tune, so every control except the panel disclosure is disabled and the
    // status explains why. Keeps the panel honest instead of offering inert
    // presets that silently do nothing.
    function setControlsDisabled(off) {
      if (!panelEl) return;
      var inputs = panelEl.querySelectorAll("input, select, button");
      Array.prototype.forEach.call(inputs, function (el2) { el2.disabled = off; });
      panelEl.classList.toggle("okf-signal--disabled", off);
    }

    // ---- Interaction wiring --------------------------------------------
    var actionBar = document.createElement("div");
    actionBar.className = "okf-graph-actions"; actionBar.hidden = true;
    actionBar.setAttribute("role", "toolbar"); actionBar.setAttribute("aria-label", "Selected concept actions");
    function addAction(label, fn) { var b=document.createElement("button");b.type="button";b.textContent=label;b.addEventListener("click",fn);actionBar.appendChild(b);return b; }
    addAction("Open page",function(){if(interactionState.selected)openConceptPage(interactionState.selected);});
    addAction("Explore neighborhood",function(){if(interactionState.selected)enterFocus(interactionState.selected,"action");});
    var isolateAction=addAction("Hide others",function(){dispatchInteraction({type:interactionState.isolated?"SHOW_ALL":"ISOLATE"});});
    var detailActionSlot=document.getElementById("okf-detail-actions-slot");
    (detailActionSlot||container).appendChild(actionBar);
    function syncActionBarOwner(){var mobile=window.matchMedia&&window.matchMedia("(max-width: 480px)").matches;var owner=mobile&&detailActionSlot?detailActionSlot:container;if(actionBar.parentNode!==owner){var active=document.activeElement,restore=actionBar.contains(active)?active:null;if(restore&&isCompactGraphViewport())setMobilePane(owner===detailActionSlot?"details":"graph",false);owner.appendChild(actionBar);actionBar.removeAttribute("inert");if(restore){restore.focus();requestAnimationFrame(function(){if(!actionBar.hidden)restore.focus();});setTimeout(function(){if(!actionBar.hidden)restore.focus();},160);}}}
    var focusBar=document.createElement("div");focusBar.className="okf-graph-focusbar";focusBar.hidden=true;focusBar.setAttribute("role","group");focusBar.setAttribute("aria-label","Explore neighborhood");
    var focusCrumb=document.createElement("strong");focusBar.appendChild(focusCrumb);
    var depthLabel=document.createElement("label");depthLabel.appendChild(document.createTextNode("Depth "));
    var focusDepth=document.createElement("select");focusDepth.setAttribute("aria-label","Neighborhood depth");
    [["1","1 hop"],["2","2 hops"]].forEach(function(o){var opt=document.createElement("option");opt.value=o[0];opt.textContent=o[1];focusDepth.appendChild(opt);});depthLabel.appendChild(focusDepth);focusBar.appendChild(depthLabel);
    var showAllFocus=document.createElement("button");showAllFocus.type="button";showAllFocus.textContent="Show all";focusBar.appendChild(showAllFocus);container.appendChild(focusBar);
    [actionBar,focusBar].forEach(function(owner){["pointerdown","mousedown","touchstart","click","dblclick"].forEach(function(name){owner.addEventListener(name,function(e){e.stopPropagation();});});});
    actionBar.addEventListener("keydown",function(e){if(e.key!=="Escape")return;e.preventDefault();e.stopPropagation();dispatchInteraction({type:"CLEAR"});var restore=canvasProxy&&canvasProxy.offsetParent!==null?canvasProxy:document.querySelector('[data-graph-pane="details"]');if(restore)restore.focus();});
    var canvasProxy=document.createElement("div");canvasProxy.className="okf-graph-canvas-proxy";canvasProxy.tabIndex=0;canvasProxy.setAttribute("role","application");canvasProxy.setAttribute("aria-label","Knowledge graph canvas");
    var shortcutHelp=document.createElement("p");shortcutHelp.className="okf-sr-only";shortcutHelp.textContent="Shortcuts: arrows move; Space selects; Shift Space sets a path endpoint; Enter opens; slash searches; f fits; Escape clears. Canvas marks are also represented in the Concepts index, detail panel, and status text; use those textual views when visual canvas distinctions are unavailable.";
    container.insertBefore(canvasProxy,container.firstChild);container.appendChild(shortcutHelp);
    var bundlePanel=document.createElement("section");bundlePanel.id="okf-edge-bundle-panel";bundlePanel.className="okf-edge-bundle-detail";bundlePanel.hidden=true;bundlePanel.setAttribute("aria-labelledby","okf-edge-bundle-title");
    var bundleIndex=document.createElement("details");bundleIndex.className="okf-edge-bundle-index";
    var bundleSummary=document.createElement("summary");bundleIndex.appendChild(bundleSummary);
    var bundleIndexList=document.createElement("div");bundleIndexList.className="okf-edge-bundle-index__list";bundleIndexList.setAttribute("role","listbox");bundleIndexList.setAttribute("aria-label","Bundled relationships");bundleIndexList.tabIndex=0;bundleIndex.appendChild(bundleIndexList);container.appendChild(bundleIndex);
    var activeBundleEdgeId=null,bundleInvokerEdgeId=null;
    function bundleButton(edgeId){return bundleIndexList.querySelector('[data-edge-id="'+CSS.escape(edgeId)+'"]');}
    function syncBundleExpanded(){Array.prototype.forEach.call(bundleIndexList.querySelectorAll("[role=option]"),function(button){button.setAttribute("aria-selected",button.dataset.edgeId===activeBundleEdgeId&&!bundlePanel.hidden?"true":"false");});}
    function collapseBundleDetail(restoreFocus){var active=document.activeElement,hadPanelFocus=bundlePanel.contains(active);bundlePanel.hidden=true;activeBundleEdgeId=null;delete bundlePanel.dataset.edgeId;cy.edges().unselect();syncBundleExpanded();if(restoreFocus||hadPanelFocus){var invoker=bundleButton(bundleInvokerEdgeId);if(invoker)bundleIndexList.setAttribute("aria-activedescendant",invoker.id);var target=bundleIndexList||bundleSummary||canvasProxy;if(target&&target.focus)target.focus();}}
    function renderBundleDetail(edge,options){options=options||{};if(!edge||!edge.length)return;var data=edge.data(),allRelations=edgeRelations(edge),relations=visibleEdgeRelations(edge),active=document.activeElement,keepFocus=bundlePanel.contains(active);while(bundlePanel.firstChild)bundlePanel.removeChild(bundlePanel.firstChild);
      var heading=document.createElement("h2");heading.id="okf-edge-bundle-title";heading.textContent="Relationships ("+relations.length+(relations.length!==allRelations.length?" of "+allRelations.length:"")+")";bundlePanel.appendChild(heading);
      var route=document.createElement("p");route.className="okf-muted";route.textContent=(edge.source().data("label")||edge.source().id())+" → "+(edge.target().data("label")||edge.target().id());bundlePanel.appendChild(route);
      var list=document.createElement("ol");list.className="okf-edge-bundle-detail__list";relations.forEach(function(relation){var item=document.createElement("li");item.dataset.relationId=relation.id;[["Type",relation.type],["Label",relation.label],["Direction",relation.direction],["Source",relation.source],["Target",relation.target],["Detail",relation.detail||""]].forEach(function(row){if(!row[1])return;var key=document.createElement("strong");key.textContent=row[0]+": ";item.appendChild(key);item.appendChild(document.createTextNode(row[1]));item.appendChild(document.createElement("br"));});list.appendChild(item);});bundlePanel.appendChild(list);
      var close=document.createElement("button");close.type="button";close.className="okf-btn";close.textContent="Collapse relationships";close.addEventListener("click",function(){collapseBundleDetail(true);});bundlePanel.appendChild(close);bundlePanel.hidden=false;bundlePanel.dataset.edgeId=edge.id();activeBundleEdgeId=edge.id();edge.select();syncBundleExpanded();if(options.focusPanel||keepFocus)close.focus();
    }
    function activateBundle(edgeId,provenance){var edge=cy.getElementById(edgeId);if(!edge||!edge.length)return;if(activeBundleEdgeId===edgeId&&!bundlePanel.hidden){collapseBundleDetail(provenance==="keyboard");return;}var pointerInvoker=bundleButton(edgeId),pointerOwned=provenance==="pointer"&&document.activeElement===pointerInvoker,detailContent=document.getElementById("detail-content");if(detailContent&&detailContent.hidden)dispatchInteraction({type:"SELECT",id:edge.source().id(),source:"bundle",quietUrl:true});var relationshipSlot=document.getElementById("detail-relationships-slot");if(relationshipSlot&&bundlePanel.parentNode!==relationshipSlot)relationshipSlot.insertBefore(bundlePanel,relationshipSlot.firstChild);if(isCompactGraphViewport())setMobilePane("details",false);bundleInvokerEdgeId=edgeId;renderBundleDetail(edge,{focusPanel:provenance==="keyboard"});if(pointerOwned&&pointerInvoker&&pointerInvoker.isConnected)pointerInvoker.focus();}
    function rebuildBundleIndex(){var focused=document.activeElement&&bundleIndexList.contains(document.activeElement)?document.activeElement.dataset.edgeId:null,rows=(bundle.edges||[]).filter(function(edge){return Number(edge.data.multiplicity)>1;}).sort(function(a,b){var as=(nodeIndex[a.data.source]&&nodeIndex[a.data.source].label)||a.data.source,bs=(nodeIndex[b.data.source]&&nodeIndex[b.data.source].label)||b.data.source,at=(nodeIndex[a.data.target]&&nodeIndex[a.data.target].label)||a.data.target,bt=(nodeIndex[b.data.target]&&nodeIndex[b.data.target].label)||b.data.target;return as.localeCompare(bs)||at.localeCompare(bt)||a.data.id.localeCompare(b.data.id);});var fragment=document.createDocumentFragment();rows.forEach(function(edgeData,rowIndex){var sourceLabel=(nodeIndex[edgeData.data.source]&&nodeIndex[edgeData.data.source].label)||edgeData.data.source,targetLabel=(nodeIndex[edgeData.data.target]&&nodeIndex[edgeData.data.target].label)||edgeData.data.target,button=document.createElement("div");button.id="okf-bundled-relationship-option-"+rowIndex;button.tabIndex=-1;button.setAttribute("role","option");button.setAttribute("aria-selected","false");button.className="okf-btn okf-edge-bundle-index__item";button.dataset.edgeId=edgeData.data.id;button.textContent=sourceLabel+" → "+targetLabel+" · "+edgeData.data.multiplicity+" relationships";button.title=edgeData.data.source+" → "+edgeData.data.target;button.setAttribute("aria-controls",bundlePanel.id);button.addEventListener("click",function(event){activateBundle(edgeData.data.id,event.detail===0?"keyboard":"pointer");});fragment.appendChild(button);});bundleIndexList.replaceChildren(fragment);bundleSummary.textContent="Bundled relationships ("+rows.length+")";bundleIndex.hidden=!rows.length;var first=bundleIndexList.querySelector('[role=option]');if(first)bundleIndexList.setAttribute("aria-activedescendant",first.id);syncBundleExpanded();stampVisibilityProjection(bundleIndex);if(focused){var restored=bundleButton(focused);if(restored)bundleIndexList.setAttribute("aria-activedescendant",restored.id);}}
    function bundleOptions(){return Array.prototype.slice.call(bundleIndexList.querySelectorAll('[role=option]'));}
    function moveBundleActive(index,open){var rows=bundleOptions();if(!rows.length)return;index=Math.max(0,Math.min(rows.length-1,index));var row=rows[index];bundleIndexList.setAttribute("aria-activedescendant",row.id);rows.forEach(function(item){item.classList.toggle("is-active",item===row);});row.scrollIntoView({block:"nearest"});if(open)activateBundle(row.dataset.edgeId,"keyboard");}
    bundleIndexList.addEventListener("focus",function(){moveBundleActive(0,false);});
    bundleIndexList.addEventListener("keydown",function(event){var rows=bundleOptions(),current=bundleIndexList.getAttribute("aria-activedescendant"),index=rows.findIndex(function(row){return row.id===current;});if(index<0)index=0;if(event.key==="ArrowDown"||event.key==="ArrowRight"){event.preventDefault();moveBundleActive(index+1,false);}else if(event.key==="ArrowUp"||event.key==="ArrowLeft"){event.preventDefault();moveBundleActive(index-1,false);}else if(event.key==="Home"){event.preventDefault();moveBundleActive(0,false);}else if(event.key==="End"){event.preventDefault();moveBundleActive(rows.length-1,false);}else if(event.key==="Enter"||event.key===" "){event.preventDefault();moveBundleActive(index,true);}else if(event.key==="Escape"){event.preventDefault();if(!bundlePanel.hidden)collapseBundleDetail(false);else clearLocalInteraction();}});
    rebuildBundleIndex();
    var relationshipSlot=document.getElementById("detail-relationships-slot");if(relationshipSlot)relationshipSlot.appendChild(bundlePanel);
    function validInteractionId(id){return typeof id==="string"&&!!nodeIndex[id];}
    function replaceRecord(target,values){Object.keys(target).forEach(function(key){delete target[key];});Object.keys(values||{}).forEach(function(key){target[key]=values[key];});return target;}
    // The toolbar sits beside the selected node and never over it: the first
    // click of a double-click selects (and shows this toolbar), and the
    // second click must still land on the node to open its page. Beside the
    // canvas's right edge it used to be clamped back over the node, so the
    // second click pressed "Explore neighborhood" instead. Prefer the right
    // (the long-standing spot), then the left, then below, then above.
    // The floating action bar sits where it hides the least (D2-015): never
    // over the selected concept or its name, never over the other canvas
    // chrome (concept list, focus bar, zoom column, minimap), and over as
    // few nearby concepts as possible. Candidates are tried above, right,
    // left and below the name; a placement that is still clear is kept while
    // the reader pans so the bar does not hop between sides.
    var actionBarSide=null;
    function positionActionBar(){var n=interactionState.selected&&cy.getElementById(interactionState.selected);if(!n||!n.length||actionBar.hidden||actionBar.parentNode!==container)return;var p=n.renderedPosition(),w=actionBar.offsetWidth,h=actionBar.offsetHeight,W=container.clientWidth,H=container.clientHeight;var off=Math.max(22,Math.max(n.renderedOuterWidth(),n.renderedOuterHeight())/2+8);
      var name=n.renderedBoundingBox({includeNodes:false,includeEdges:false,includeLabels:true}),below=Math.max(p.y+off,name.y2+8);
      var c=container.getBoundingClientRect(),chrome=[];Array.prototype.forEach.call(container.querySelectorAll(".okf-node-index > summary, .okf-graph-focusbar:not([hidden]), .okf-graph-zoom, .okf-graph-minimap:not([hidden]), .okf-graph-legend:not([hidden])"),function(el){var r=el.getBoundingClientRect();if(r.width&&r.height)chrome.push({x1:r.left-c.left-4,y1:r.top-c.top-4,x2:r.right-c.left+4,y2:r.bottom-c.top+4});});
      var near=[],reach=Math.max(w,h)+off+40;cy.nodes(":visible").forEach(function(m){if(m.same(n))return;var q=m.renderedPosition();if(Math.abs(q.x-p.x)>reach+w||Math.abs(q.y-p.y)>reach+h)return;near.push(m.renderedBoundingBox({includeLabels:!m.hasClass("okf-label-culled"),includeEdges:false,includeOverlays:false}));});
      function hits(a,b){return a.x1<b.x2&&b.x1<a.x2&&a.y1<b.y2&&b.y1<a.y2;}
      var own={x1:Math.min(p.x-off,name.x1),y1:p.y-off,x2:Math.max(p.x+off,name.x2),y2:name.y2};
      var candidates=[["above",p.x-w/2,p.y-off-h],["right",p.x+off,p.y-h/2],["left",p.x-off-w,p.y-h/2],["below",p.x-w/2,below],["above-right",p.x-off,p.y-off-h],["above-left",p.x+off-w,p.y-off-h],["below-right",p.x-off,below],["below-left",p.x+off-w,below]],best=null;
      candidates.forEach(function(cand,order){var left=Math.max(8,Math.min(W-w-8,cand[1])),top=Math.max(8,Math.min(H-h-8,cand[2])),box={x1:left,y1:top,x2:left+w,y2:top+h};var score=(hits(box,own)?1000:0)+chrome.filter(function(r){return hits(box,r);}).length*100+near.filter(function(r){return hits(box,r);}).length*10+order+(left!==cand[1]||top!==cand[2]?5:0);if(cand[0]===actionBarSide&&score<100)score-=20;if(!best||score<best.score)best={side:cand[0],left:left,top:top,score:score};});
      actionBarSide=best.side;actionBar.dataset.side=best.side;actionBar.style.left=best.left+"px";actionBar.style.top=best.top+"px";}
    // The bar follows pan, zoom and its own concept at once; other concepts
    // moving (a running layout fires one event per node) only change which
    // side is clearest, so those re-placements coalesce to one per frame.
    var actionBarFrame=0;cy.on("pan zoom",positionActionBar);cy.on("position",function(evt){if(actionBar.hidden)return;if(evt.target&&evt.target.id&&evt.target.id()===interactionState.selected){positionActionBar();return;}if(actionBarFrame)return;actionBarFrame=requestAnimationFrame(function(){actionBarFrame=0;if(!cy.destroyed())positionActionBar();});});window.addEventListener("resize",function(){syncActionBarOwner();positionActionBar();});
    var graphResizeTimer=null,userViewportInteracted=false;
    ["wheel","pointerdown","touchstart"].forEach(function(name){container.addEventListener(name,function(event){if(event.isTrusted&&(event.target.tagName==="CANVAS"||event.target===canvasProxy))userViewportInteracted=true;},{passive:true});});
    function refitGraphViewport(){if(graphResizeTimer)cancelAnimationFrame(graphResizeTimer);graphResizeTimer=requestAnimationFrame(function(){graphResizeTimer=null;if(graphPageActive&&layoutSettled&&container.offsetWidth&&container.offsetHeight){cy.resize();if(!userViewportInteracted){cancelPendingViewportRestore();syncMobileReadableStyle();compactMobileSmallGraph();overlayAwareFit();}positionActionBar();syncMinimap();}});}
    window.addEventListener("resize",refitGraphViewport);window.addEventListener("okf-loom:graph-pane",function(event){if(event.detail&&event.detail.pane==="graph")refitGraphViewport();});
    var canvasSizeObserver = typeof ResizeObserver !== "undefined" ? new ResizeObserver(refitGraphViewport) : null;
    if (canvasSizeObserver) canvasSizeObserver.observe(container);
    window.addEventListener("pagehide", function () { if (canvasSizeObserver) canvasSizeObserver.disconnect(); if (graphResizeTimer) cancelAnimationFrame(graphResizeTimer); });
    window.addEventListener("pageshow", function (event) { if (event.persisted && canvasSizeObserver) canvasSizeObserver.observe(container); });
    function syncInteractionIndex(){var list=document.getElementById("okf-node-index-list"),activeOption=null;Array.prototype.forEach.call(document.querySelectorAll("#okf-node-index-list .okf-node-index__item"),function(b){var active=b.dataset.target===interactionState.active,selected=b.dataset.target===interactionState.selected;b.classList.toggle("is-active",active);b.setAttribute("aria-selected",selected?"true":"false");if(active)activeOption=b;});if(list&&activeOption)list.setAttribute("aria-activedescendant",activeOption.id);}
    function renderInteraction(pos){syncActionBarOwner();applyFilters();applyEdgeLabels();applyEdgeElision();renderRelationshipProjection();if(activeBundleEdgeId&&!bundlePanel.hidden){var activeEdge=cy.getElementById(activeBundleEdgeId);if(activeEdge&&activeEdge.length)renderBundleDetail(activeEdge,{focusPanel:false});}syncInteractionIndex();cy.edges().removeClass("okf-unrelated");if(interactionState.selected){var selectedNode=cy.getElementById(interactionState.selected);if(selectedNode.length)cy.edges().not(selectedNode.connectedEdges()).addClass("okf-unrelated");}cy.elements().removeClass("okf-hover-dim");cy.nodes().removeClass("okf-hover");if(interactionState.hovered){var n=cy.getElementById(interactionState.hovered);if(n.length){cy.elements().not(n.closedNeighborhood()).addClass("okf-hover-dim");n.addClass("okf-hover");showHoverTip(n,pos||n.renderedPosition());}}actionBar.hidden=!interactionState.selected;isolateAction.textContent=interactionState.isolated?"Show all":"Hide others";positionActionBar();focusBar.hidden=!(controlState.focusEnabled&&focusRoot);container.classList.toggle("okf-graph-focus-active",!focusBar.hidden);if(!focusBar.hidden){focusCrumb.textContent="Exploring neighborhood around "+((nodeIndex[focusRoot]&&nodeIndex[focusRoot].label)||focusRoot);focusDepth.value=String(controlState.focusDepth);}scheduleLabelCull();publishInteractionSnapshot("render");}
    function restoreInvokerFocus(){var id=interactionState.focusInvoker,target=null,list=document.getElementById("okf-node-index-list");Array.prototype.some.call(document.querySelectorAll("#okf-node-index-list .okf-node-index__item"),function(option){if(option.dataset.target===id){target=option;return true;}return false;});if(id)dispatchInteraction({type:"ACTIVE",id:id,quietUrl:true,source:"neighborhood-restore"});if(target&&target.offsetParent!==null&&list){list.setAttribute("aria-activedescendant",target.id);list.focus();}else canvasProxy.focus();}
    function captureFocusOrigin(id){if(!interactionState.priorLens){var pan=cy.pan();interactionState.priorLens=controlState.lens!=="focus"?controlState.lens:"map";interactionState.priorViewport={zoom:cy.zoom(),pan:{x:pan.x,y:pan.y}};}interactionState.focusInvoker=id||interactionState.focusInvoker;}
    function activateFocusLens(source){captureFocusOrigin(interactionState.selected);if(interactionState.selected)enterFocus(interactionState.selected,source);else{applyLens("focus");renderInteraction();writeInteractionUrl("push");}}
    function enterFocus(id,source){if(!validInteractionId(id))return;cancelPendingViewportRestore();captureFocusOrigin(id);focusRoot=id;interactionState.isolated=null;var requestedDepth=controlState.focusDepth===2?2:1;applyLens("focus",true);controlState.focusDepth=requestedDepth;syncControlsFromState();applyFilters();renderInteraction();runLayoutNow(true,false,source!=="url");if(source!=="url")writeInteractionUrl("push");}
    function exitFocus(options){options=options||{};if(!controlState.focusEnabled&&!focusRoot)return;var lens=interactionState.priorLens&&LENSES[interactionState.priorLens]?interactionState.priorLens:"map",vp=interactionState.priorViewport;focusRoot=null;controlState.focusEnabled=false;pendingFocusRestore=options.restoreFocus!==false;applyLens(lens);if(vp)pendingViewportRestore={viewport:vp,generation:++viewportRestoreGeneration,phase:"layout"};applyFilters();renderInteraction();runLayoutNow(true,true,options.animate!==false);interactionState.priorLens=null;interactionState.priorViewport=null;if(!options.quietUrl)writeInteractionUrl("replace");}
    function reconcileDeletedFocus() { if(!controlState.focusEnabled&&!focusRoot)return;focusRoot=null;controlState.focusEnabled=false;pendingFocusRestore=false;cancelPendingViewportRestore();var lens=interactionState.priorLens&&LENSES[interactionState.priorLens]?interactionState.priorLens:"map";controlState.lens=lens;interactionState.priorLens=null;interactionState.priorViewport=null;syncControlsFromState();applyFilters();renderInteraction(); }
    focusDepth.addEventListener("change",function(){controlState.focusDepth=Number(focusDepth.value)===2?2:1;if(ui.depth)ui.depth.value=String(controlState.focusDepth);applyFilters();runLayoutNow(true,false,true);renderInteraction();writeInteractionUrl("replace");});
    showAllFocus.addEventListener("click",function(){exitFocus();});
    var restoringInteractionUrl=false;
    function writeInteractionUrl(mode){if(restoringInteractionUrl||!history||!window.URL)return;var u=new URL(location.href),p=u.searchParams;["selected","lens","focus","focusDepth","path","hidden","hiddenRelation"].forEach(function(k){p.delete(k);});if(interactionState.selected)p.set("selected",interactionState.selected);if(controlState.lens!=="map")p.set("lens",controlState.lens);if(focusRoot){p.set("focus",focusRoot);p.set("focusDepth",String(controlState.focusDepth));}if(interactionState.pathStart&&interactionState.pathEnd)p.set("path",JSON.stringify([interactionState.pathStart,interactionState.pathEnd]));var hidden=Object.keys(controlState.hiddenTypes).filter(function(t){return controlState.hiddenTypes[t]&&palette[t];}).sort();hidden.forEach(function(t){p.append("hidden",t);});var relationTypes={};cy.edges().forEach(function(e){edgeRelations(e).forEach(function(relation){if(relation.type)relationTypes[relation.type]=true;});});Object.keys(controlState.hiddenRelations).filter(function(r){return controlState.hiddenRelations[r]&&relationTypes[r];}).sort().forEach(function(r){p.append("hiddenRelation",r);});history[mode==="push"?"pushState":"replaceState"]({},"",u.pathname+(p.toString()?"?"+p.toString():"")+u.hash);}
    function dispatchInteraction(a){if(a.type==="FILTER_SEARCH"){controlState.search=String(a.value||"").trim().toLowerCase();updateStatus();}else if(a.type==="FILTER_TYPE"){controlState.type=String(a.value||"");updateStatus();}else if(a.type==="LENS"&&LENSES[a.name]){projectLens(a.name,a.focusCanonical);}else if(a.type==="SELECT"&&validInteractionId(a.id)){interactionState.selected=a.id;interactionState.active=a.id;if(a.source!=="arrow"||!validInteractionId(interactionState.pathStart))interactionState.pathStart=a.id;showDetail(a.id,a.source||"pointer");if(controlState.lens==="focus"&&!focusRoot)enterFocus(a.id,a.source||"selection");}else if(a.type==="ACTIVE"&&validInteractionId(a.id)){interactionState.active=a.id;syncInteractionIndex();publishInteractionSnapshot(a.source||"active");return;}else if(a.type==="HOVER"){if(hoverTimer)clearTimeout(hoverTimer);if(hoverTransitionTimer)clearTimeout(hoverTransitionTimer);hoverTransitionTimer=null;var own=++interactionState.hoverGeneration;hoverTimer=setTimeout(function(){hoverTimer=null;if(own!==interactionState.hoverGeneration||!graphPageActive)return;interactionState.hovered=a.id;hoveredNodeId=a.id;renderInteraction(a.position);if(!REDUCED_MOTION)hoverTransitionTimer=setTimeout(function(){if(own===interactionState.hoverGeneration)hoverTransitionTimer=null;},120);},120);return;}else if(a.type==="UNHOVER"){interactionState.hoverGeneration++;if(hoverTimer)clearTimeout(hoverTimer);if(hoverTransitionTimer)clearTimeout(hoverTransitionTimer);hoverTimer=null;hoverTransitionTimer=null;interactionState.hovered=null;hoveredNodeId=null;hideHoverTip();renderInteraction();return;}else if(a.type==="PATH"&&validInteractionId(a.id)){var from=validInteractionId(a.from)?a.from:interactionState.pathStart;if(validInteractionId(from)){showPathBetween(cy.getElementById(from),cy.getElementById(a.id));interactionState.pathStart=from;interactionState.pathEnd=a.id;}}else if(a.type==="ISOLATE"&&interactionState.selected)interactionState.isolated=interactionState.selected;else if(a.type==="SHOW_ALL")interactionState.isolated=null;else if(a.type==="CLEAR"){interactionState.selected=null;interactionState.active=null;interactionState.pathStart=null;interactionState.pathEnd=null;interactionState.isolated=null;clearPath();clearSelection({preserveFocus:controlState.focusEnabled&&validInteractionId(focusRoot)});}renderInteraction();if(!a.quietUrl)writeInteractionUrl(a.type==="SELECT"?"push":"replace");}
    var graphInteractionController=Object.freeze({dispatch:dispatchInteraction,snapshot:graphInteractionSnapshot,restore:restoreInteractionUrl});
    function decodeInteractionUrl(){var p=new URL(location.href).searchParams,s={lens:"map",selected:null,focus:null,focusDepth:1,path:null,hidden:{},hiddenRelations:{}};var lens=p.get("lens");if(lens&&LENSES[lens])s.lens=lens;var selected=p.get("selected");if(validInteractionId(selected))s.selected=selected;var focus=p.get("focus");if(validInteractionId(focus)){s.focus=focus;s.focusDepth=p.get("focusDepth")==="2"?2:1;}try{var path=JSON.parse(p.get("path")||"null");if(Array.isArray(path)&&path.length===2&&validInteractionId(path[0])&&validInteractionId(path[1]))s.path=path;}catch(e){}p.getAll("hidden").forEach(function(t){if(palette[t])s.hidden[t]=true;});if(!filterStateHasVisibleInstance("Type",s.hidden))s.hidden={};var relationTypes={};cy.edges().forEach(function(e){edgeRelations(e).forEach(function(relation){if(relation.type)relationTypes[relation.type]=true;});});p.getAll("hiddenRelation").forEach(function(r){if(relationTypes[r])s.hiddenRelations[r]=true;});if(!filterStateHasVisibleInstance("Relationship",s.hiddenRelations))s.hiddenRelations={};return s;}
    function restoreInteractionUrl(){var s=decodeInteractionUrl();restoringInteractionUrl=true;cancelPendingViewportRestore();pendingFocusRestore=false;try{clearPath();focusRoot=null;controlState.focusEnabled=false;interactionState.selected=null;interactionState.active=null;interactionState.isolated=null;replaceRecord(controlState.hiddenTypes,s.hidden);replaceRecord(controlState.hiddenRelations,s.hiddenRelations);applyLens(s.lens);
if(s.selected){
  dispatchInteraction({type:"SELECT",id:s.selected,source:"url",quietUrl:true});
  if(location.hash.length>1){
    var pendingFragment="";try{pendingFragment=decodeURIComponent(location.hash.slice(1));}catch(hashError){pendingFragment=location.hash.slice(1);}
    var ownedIntent=null;try{ownedIntent=(history.state&&history.state.okfSectionIntent)||null;}catch(stateError){ownedIntent=null;}
    /* OWNED section entry (minted by a section action): the strict
     * destination admission applies again on every traversal — the SAME
     * independently parsed destination revision map as initial activation
     * (exact entry for the selected canonical concept, exact revision),
     * then label re-proof inside the displayed body. An owned intent
     * restored against a rebuilt artifact whose source moved, or without
     * the destination map, is stale proof: the owned fragment is cleared
     * WITHOUT a history push and the restored selection keeps root reading
     * — it is never routed through the ordinary legacy-hash focus. */
    if(ownedIntent&&typeof ownedIntent==="object"&&ownedIntent.fragment===pendingFragment
        &&(ownedIntent.heading==null||typeof ownedIntent.heading==="string")
        &&singleSourceRevisions!==null
        &&Object.prototype.hasOwnProperty.call(singleSourceRevisions,s.selected)
        &&singleSourceRevisions[s.selected]===ownedIntent.revision){
      focusAdmittedDetailFragment(document.getElementById("detail-body"),pendingFragment,ownedIntent.heading==null?null:String(ownedIntent.heading));
    }else if(ownedIntent){
      stripDetailHash();
    }else{
      /* Ordinary document-local hash (source footnotes, back links, the
       * #detail-content concept-list deep link): the shipped focus path —
       * never a strip, never treated as a section action. */
      focusDetailFragment(document.getElementById("detail-body"),pendingFragment,null);
    }
  }
}else clearSelection();
if(s.focus){controlState.focusDepth=s.focusDepth;enterFocus(s.focus,"url");}
if(s.path)dispatchInteraction({type:"PATH",from:s.path[0],id:s.path[1],quietUrl:true});renderInteraction();reconcileVisibleRelationshipProjection();}finally{restoringInteractionUrl=false;}}
    canvasProxy.addEventListener("focus",function(){if(!interactionState.active&&bundle.nodes.length)dispatchInteraction({type:"ACTIVE",id:bundle.nodes.slice().sort(function(a,b){return String(a.data.id).localeCompare(String(b.data.id));})[0].data.id,quietUrl:true});});
    window.addEventListener("popstate",graphInteractionController.restore);
    window.addEventListener("pagehide",function(){interactionState.hoverGeneration++;if(hoverTimer)clearTimeout(hoverTimer);if(hoverTransitionTimer)clearTimeout(hoverTransitionTimer);hoverTimer=null;hoverTransitionTimer=null;interactionState.hovered=null;fullscreenGeneration++;fullscreenPending=false;cancelPendingViewportRestore();hideHoverTip();renderInteraction();actionBar.hidden=true;focusBar.hidden=true;});
    // Phase 4: tap selects (detail panel); shift-tap traces a path from
    // the selected node; double-tap opens the concept page; background tap
    // clears everything.
    var recentTapNodes=[];
    cy.on("tap", "node", function (evt) {
      var priorSelected=interactionState.selected;
      recentTapNodes.push(evt.target.id());if(recentTapNodes.length>2)recentTapNodes.shift();
      var oe = evt.originalEvent;
      var sel = priorSelected&&validInteractionId(priorSelected)?cy.getElementById(priorSelected):cy.nodes(":selected");
      var shifted=!!(oe&&(oe.shiftKey||(typeof oe.getModifierState==="function"&&oe.getModifierState("Shift"))||(oe.originalEvent&&(oe.originalEvent.shiftKey||(typeof oe.originalEvent.getModifierState==="function"&&oe.originalEvent.getModifierState("Shift"))))));
      if (shifted && sel.length) {
        dispatchInteraction({type:"PATH",from:sel.id(),id:evt.target.id()});
        return;
      }
      clearPath();
      dispatchInteraction({type:"SELECT",id:evt.target.id(),source:"pointer"});
    });
    cy.on("tap", "edge", function(evt){evt.stopPropagation();activateBundle(evt.target.id(),"pointer");});
    function clearGraphBackground(){dispatchInteraction({type:"CLEAR"});}
    cy.on("tap", function (evt) {
      if(evt.target===cy)clearGraphBackground();
    });
    // A synthetic no-payload core tap is the supported test/diagnostic clear
    // seam. Native Cytoscape returns its cyclic core object; serializing that
    // return through browser automation can exhaust Chrome. Route the same
    // clear owner while returning no serializable graph object.
    var nativeCoreTrigger=cy.trigger;
    cy.trigger=function(events,extra){if(events==="tap"&&arguments.length===1){clearGraphBackground();return;}return nativeCoreTrigger.apply(cy,arguments);};
    cy.on("dbltap", "node", function (evt) {
      if(recentTapNodes.length===2&&recentTapNodes[0]===evt.target.id()&&recentTapNodes[1]===evt.target.id())openConceptPage(evt.target.id());
    });

    function openConceptPage(conceptId) {
      var openSuffix = (MODE === "static") ? ".html" : "";
      window.location.href = conceptPagePrefix.replace(/\/$/, "") + "/" + conceptId + openSuffix;
    }
    function clearLocalInteraction() {
      if (interactionState.pathEnd) { clearPath(); writeInteractionUrl("replace"); }
      else if (controlState.search) { var search=document.getElementById("okf-search"); if(search)search.value=""; dispatchInteraction({type:"FILTER_SEARCH",value:"",quietUrl:true}); writeInteractionUrl("replace"); }
      else if (controlState.focusEnabled && focusRoot) exitFocus();
      else dispatchInteraction({type:"CLEAR"});
      updateStatus();
    }

    // ---- Phase 4: hover = preview --------------------------------------
    // The pointed node's neighborhood keeps full opacity while the rest
    // fades, and a tooltip card (type, title, description, link counts)
    // follows the cursor. Filters/Focus own `.dim`; hover uses its own
    // class so the two dim sources never fight.
    var hoverTipEl = null;
    function ensureHoverTip() {
      if (hoverTipEl) return hoverTipEl;
      hoverTipEl = document.createElement("div");
      hoverTipEl.className = "okf-graph-tip";
      hoverTipEl.setAttribute("role", "tooltip");
      hoverTipEl.hidden = true;
      container.appendChild(hoverTipEl);
      return hoverTipEl;
    }
    function showHoverTip(node, rpos) {
      var tip = ensureHoverTip();
      var d = node.data();
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      var typeRow = document.createElement("div");
      typeRow.className = "okf-graph-tip__type";
      typeRow.textContent = d.type || "concept";
      // The node's canvas fill is a stripe beside the name (graph.css), not
      // the name's ink: neutral by default, the muted lens colour otherwise.
      typeRow.style.setProperty("--okf-graph-tip-fill", d.color || d.baseColor || "");
      tip.appendChild(typeRow);
      var title = document.createElement("div");
      title.className = "okf-graph-tip__title";
      title.textContent = d.label || node.id();
      tip.appendChild(title);
      if (d.description) {
        var desc = document.createElement("div");
        desc.className = "okf-graph-tip__desc";
        var text = String(d.description);
        desc.textContent = text.length > 140 ? text.slice(0, 140) + "…" : text;
        tip.appendChild(desc);
      }
        var meta = document.createElement("div");
        meta.className = "okf-graph-tip__meta";
        // P1 collision fix: when colour-by-theme is active, surface the theme
        // label (T1, T2…) so the tooltip carries non-colour theme identity
        // alongside the type row. Kept to one short token to avoid clutter.
        var themeBit = "";
        if (controlState.colorMode === "community" && communityOf[node.id()] != null) {
          themeBit = themeLabel(communityOf[node.id()]) + " · ";
        }
        meta.textContent = themeBit + "→ " + (outgoing[node.id()] || []).length +
          " · ← " + (backlinks[node.id()] || []).length + " · double-click opens";
        // Phase 3: surface the deterministic broken-link count in the
        // tooltip so the warning is readable on hover (non-colour channel
        // for the canvas underlay cue). data.unresolved_count is shipped
        // server-side from Graph.unresolved.
        var unres = Number(d.unresolved_count) || 0;
        if (unres > 0) {
          var warn = document.createElement("div");
          warn.className = "okf-graph-tip__warn";
          warn.textContent = "\u26a0 " + unres + " broken link" + (unres === 1 ? "" : "s");
          tip.appendChild(warn);
        }
      tip.appendChild(meta);
      tip.hidden = false;
      positionHoverTip(rpos);
    }
    function positionHoverTip(rpos) {
      if (!hoverTipEl || hoverTipEl.hidden || !rpos) return;
      var pad = 14;
      var w = hoverTipEl.offsetWidth, h = hoverTipEl.offsetHeight;
      var x = Math.min(Math.max(8, rpos.x + pad), Math.max(8, container.clientWidth - w - 8));
      var y = rpos.y - h - pad;
      if (y < 8) y = rpos.y + pad;
      hoverTipEl.style.left = x + "px";
      hoverTipEl.style.top = y + "px";
    }
    function hideHoverTip() { if (hoverTipEl) hoverTipEl.hidden = true; }
    function showEdgeTip(edge, rpos) {
      var tip=ensureHoverTip();while(tip.firstChild)tip.removeChild(tip.firstChild);
      var kind=document.createElement("div");kind.className="okf-graph-tip__type";kind.textContent="Relationship";
      var title=document.createElement("div");title.className="okf-graph-tip__title";title.textContent=edgeRelationValues(edge).join(" · ")||"related";
      var meta=document.createElement("div");meta.className="okf-graph-tip__meta";meta.textContent=(edge.source().data("label")||edge.source().id())+" → "+(edge.target().data("label")||edge.target().id());
      tip.appendChild(kind);tip.appendChild(title);tip.appendChild(meta);tip.hidden=false;positionHoverTip(rpos);
    }

      cy.on("mouseover", "node", function (evt) {
        var n = evt.target;
        container.style.cursor = "pointer";
        dispatchInteraction({type:"HOVER",id:n.id(),position:evt.renderedPosition});
      });
      cy.on("mouseout", "node", function (evt) {
        container.style.cursor = "";
        dispatchInteraction({type:"UNHOVER",quietUrl:true});
      });
    cy.on("mousemove", "node", function (evt) { positionHoverTip(evt.renderedPosition); });
    // Directional edge flow has one requestAnimationFrame owner. Geometry is
    // never touched: only Cytoscape's line-dash-offset changes. Selection and
    // pointer/keyboard focus feed the same active set; capture, reduced motion,
    // page departure and element removal synchronously return to the stable cue.
    var edgeFlowFrame=0,edgeFlowEpoch=0,edgeFlowHovered=null,edgeFlowLeaseGeneration=0,edgeFlowLeaseToken=null;
    function directionalEdge(edge){return edge&&edge.length&&!edge.removed()&&(edge.hasClass("okf-rel-edge")||!!edge.data("label"));}
    function edgeFlowSet(){var active=cy.edges(":selected");if(edgeFlowHovered&&directionalEdge(edgeFlowHovered))active=active.union(edgeFlowHovered);return active.filter(function(e){return directionalEdge(e);});}
    function stopDirectionalEdgeFlow(){if(edgeFlowFrame){cancelAnimationFrame(edgeFlowFrame);edgeFlowFrame=0;}edgeFlowEpoch++;try{cy.edges().style("line-dash-offset",0);}catch(e){}document.body.removeAttribute("data-okf-edge-flow");}
    function tickDirectionalEdgeFlow(now,epoch){if(epoch!==edgeFlowEpoch||REDUCED_MOTION||edgeFlowLeaseToken||document.hidden)return stopDirectionalEdgeFlow();var edges=edgeFlowSet();if(!edges.length)return stopDirectionalEdgeFlow();edges.style("line-dash-offset",-Math.floor(now/32)%18);document.body.setAttribute("data-okf-edge-flow","active");edgeFlowFrame=requestAnimationFrame(function(next){tickDirectionalEdgeFlow(next,epoch);});}
    // The hovered or selected directional edge is dashed while it is the
    // flow target, so the moving dash shows direction (and, with reduced
    // motion, the dash itself is the static cue). Plain links draw solid
    // otherwise; typed relations keep their own dash pattern.
    function syncDirectionalEdgeFlow(){stopDirectionalEdgeFlow();var flowing=edgeFlowSet();cy.edges(".okf-edge-flow").difference(flowing).removeClass("okf-edge-flow");flowing.addClass("okf-edge-flow");if(!REDUCED_MOTION&&!edgeFlowLeaseToken&&edgeFlowSet().length){var epoch=edgeFlowEpoch;edgeFlowFrame=requestAnimationFrame(function(now){tickDirectionalEdgeFlow(now,epoch);});}}
    function acquireDirectionalEdgeFlowLease(){var token="edge-flow-capture-"+(++edgeFlowLeaseGeneration);edgeFlowLeaseToken=token;stopDirectionalEdgeFlow();return token;}
    function releaseDirectionalEdgeFlowLease(token){if(token!==edgeFlowLeaseToken)return false;edgeFlowLeaseToken=null;if(!document.hidden)syncDirectionalEdgeFlow();return true;}
    function invalidateDirectionalEdgeFlowLease(){edgeFlowLeaseGeneration++;edgeFlowLeaseToken=null;stopDirectionalEdgeFlow();}
    // Edge hover reveals its relationship label; mouseout restores the
    // canonical label state.
    cy.on("mouseover", "edge", function (evt) {
      edgeFlowHovered=evt.target;syncDirectionalEdgeFlow();
      if (evt.target.data("label")) { if(!FORCED_COLORS)evt.target.addClass("okf-show-label");showEdgeTip(evt.target,evt.renderedPosition); }
    });
    cy.on("mouseout", "edge", function () { edgeFlowHovered=null;syncDirectionalEdgeFlow();hideHoverTip(); applyEdgeLabels(); });
    cy.on("select unselect", "edge", syncDirectionalEdgeFlow);
    cy.on("remove", "edge", function(evt){if(edgeFlowHovered&&edgeFlowHovered.id()===evt.target.id())edgeFlowHovered=null;syncDirectionalEdgeFlow();});
    window.addEventListener("pagehide",invalidateDirectionalEdgeFlowLease);
    window.addEventListener("blur",stopDirectionalEdgeFlow);
    window.addEventListener("focus",syncDirectionalEdgeFlow);
    document.addEventListener("visibilitychange",function(){if(document.hidden)stopDirectionalEdgeFlow();else syncDirectionalEdgeFlow();});
    if(reducedMotionQuery){var edgeFlowMotion=function(){if(REDUCED_MOTION)stopDirectionalEdgeFlow();else syncDirectionalEdgeFlow();};if(reducedMotionQuery.addEventListener)reducedMotionQuery.addEventListener("change",edgeFlowMotion);else if(reducedMotionQuery.addListener)reducedMotionQuery.addListener(edgeFlowMotion);}
    // Pan/zoom/drag invalidate the tooltip's anchor.
    cy.on("pan zoom drag", function () { hideHoverTip(); });

    // ---- Phase 4: path tracing (shift-click) ----------------------------
    // Select node A, shift-click node B → the shortest chain lights up and
    // the status line reads it out ("Orders —derived_from→ Subscriptions").
    var pathEles = null;
    var pathPanel = null;
    function setStatusNote(msg) {
      if (!statusEl) return;
      if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
      statusEl.textContent = msg;
    }
      function clearPath() {
        if (pathEles) { pathEles.removeClass("okf-path"); pathEles = null; }
        interactionState.pathEnd = null; interactionState.pathResult = null;
        cy.elements().removeClass("okf-path-dim");
        if(pathPanel)pathPanel.remove();pathPanel=null;
        // P1 progressive disclosure: re-elide edges now that no path is shown.
        applyEdgeElision();
      }
      function renderPathPanel(result) {
        var content=document.getElementById("detail-content");if(!content)return;
        if(pathPanel)pathPanel.remove();pathPanel=document.createElement("section");pathPanel.className="okf-detail-path";pathPanel.setAttribute("aria-labelledby","okf-detail-path-title");
        var h=document.createElement("h3");h.id="okf-detail-path-title";h.textContent="Connection path";pathPanel.appendChild(h);
        var message=document.createElement("p");message.className="okf-detail-path__message";message.textContent=result.message;pathPanel.appendChild(message);
        if(result.steps&&result.steps.length){var ol=document.createElement("ol");ol.className="okf-detail-path__chain";result.steps.forEach(function(step,i){var li=document.createElement("li");var node=document.createElement("button");node.type="button";node.className="okf-detail-path__node";node.textContent=step.label;node.addEventListener("click",function(){dispatchInteraction({type:"SELECT",id:step.id,source:"pointer"});});li.appendChild(node);if(i<result.relations.length){var relation=result.relations[i],rel=document.createElement("span");rel.className="okf-detail-path__relation okf-detail-path__relation--"+relation.orientation;rel.textContent=(relation.orientation==="reverse"?"← reverse ":"→ ")+(relation.label||"related");rel.setAttribute("aria-label",relation.orientation==="reverse"?"Traversed in reverse: "+relation.label:"Traversed forward: "+relation.label);li.appendChild(rel);}ol.appendChild(li);});pathPanel.appendChild(ol);}
        var clear=document.createElement("button");clear.type="button";clear.className="okf-signal__btn";clear.textContent="Clear path";clear.addEventListener("click",function(){clearPath();writeInteractionUrl("replace");canvasProxy.focus();});pathPanel.appendChild(clear);
        content.insertBefore(pathPanel,content.firstChild);
      }
      function deterministicVisiblePath(a,b){
        if(a.id()===b.id())return {kind:"same",nodes:[a.id()],edges:[]};
        if(a.hasClass("dim")||b.hasClass("dim"))return {kind:"filtered",nodes:[],edges:[]};
        var queue=[a.id()],seen={};seen[a.id()]=true;var prev={},via={};
        while(queue.length){var id=queue.shift();if(id===b.id())break;var n=cy.getElementById(id),next=[];n.connectedEdges().not(".dim").forEach(function(e){var other=e.source().id()===id?e.target():e.source();if(!other.hasClass("dim"))next.push({id:other.id(),edge:e});});next.sort(function(x,y){var c=x.id.localeCompare(y.id);return c||x.edge.id().localeCompare(y.edge.id());});next.forEach(function(x){if(!seen[x.id]){seen[x.id]=true;prev[x.id]=id;via[x.id]=x.edge.id();queue.push(x.id);}});}
        if(!seen[b.id()])return {kind:"disconnected",nodes:[],edges:[]};
        var nodes=[b.id()],edges=[];while(nodes[0]!==a.id()){var cur=nodes[0],prior=prev[cur],edge=cy.getElementById(via[cur]);edges.unshift({id:via[cur],orientation:edge.source().id()===prior&&edge.target().id()===cur?"forward":"reverse"});nodes.unshift(prior);}return {kind:"found",nodes:nodes,edges:edges};
      }
      function showPathBetween(a, b) {
        clearPath();
        var res=deterministicVisiblePath(a,b),al=a.data("label")||a.id(),bl=b.data("label")||b.id();
        if(res.kind!=="found"){
          var msg=res.kind==="same"?"Start and end are the same concept.":res.kind==="filtered"?"Path unavailable because an endpoint is hidden by the current filters.":"No visible path connects “"+al+"” and “"+bl+"”.";
          interactionState.pathResult={kind:res.kind,message:msg};renderPathPanel(interactionState.pathResult);setStatusNote(msg);return false;
        }
        var collection=cy.collection();res.nodes.forEach(function(id){collection=collection.union(cy.getElementById(id));});res.edges.forEach(function(step){collection=collection.union(cy.getElementById(step.id));});pathEles=collection;
        // P1 progressive disclosure: reveal path edges (some may be elided
        // backbone edges) before the .okf-path highlight paints.
        applyEdgeElision();
        cy.elements().not(pathEles).addClass("okf-path-dim");pathEles.addClass("okf-path");
        var steps=res.nodes.map(function(id){return{id:id,label:(nodeIndex[id]&&nodeIndex[id].label)||id};});var relations=res.edges.map(function(step){var edge=cy.getElementById(step.id);return{id:step.id,label:visibleEdgeRelationValues(edge).join(" / ")||"related",orientation:step.orientation};});
        var concise=[];steps.forEach(function(s,i){concise.push(s.label);if(relations[i])concise.push((relations[i].orientation==="reverse"?"reverse via ":"forward via ")+relations[i].label);});
        interactionState.pathResult={kind:"found",steps:steps,relations:relations,message:"Shortest visible path with "+res.edges.length+" connection"+(res.edges.length===1?"":"s")+"."};renderPathPanel(interactionState.pathResult);setStatusNote("Path: "+concise.join(" → "));return true;
    }

    // ---- Phase 4: keyboard ----------------------------------------------
    // f fits, / focuses search, Esc clears, Enter opens the selection,
    // arrows walk to the nearest neighbour in that direction.
    // KEYBOARD_FOCUS_MATRIX non-owned rows (expected preventDefault: false):
    // Copy, Ctrl/Cmd+D, Delete, Backspace | canvas proxy + node index |
    // editable + header/chrome + dialog/action controls | browser/native owner |
    // no graph mutation | proof: test_phase4_final_non_owned_keyboard_matrix.
    // Alt/Option filter click | filter chip owner | preventDefault: false |
    // same result as visible “Only” button | proof: final filter matrix test.
    // No destructive graph shortcut exists: Delete/Backspace always remain native.
    function isTypingTarget(t) {
      // Any interactive element owns its own keyboard semantics (the node
      // index buttons, preset radios, zoom cluster, links…) — the canvas
      // shortcuts only fire when focus rests on the page/canvas itself.
      if (!t || t === document.body) return false;
      var tag = (t.tagName || "").toLowerCase();
      return tag === "input" || tag === "textarea" || tag === "select" ||
        tag === "button" || tag === "a" || tag === "summary" || tag === "label" ||
        t.isContentEditable || t.getAttribute("role") === "button" ||
        t.hasAttribute("tabindex");
    }
    function walkSelection(dx, dy) {
      var sel = cy.nodes(":selected");
      if (!sel.length) return;
      var p = sel.position();
      var best = null, bestScore = Infinity;
      sel.closedNeighborhood().nodes().not(sel).forEach(function (n) {
        var q = n.position();
        var vx = q.x - p.x, vy = q.y - p.y;
        var along = vx * dx + vy * dy;             // progress in the pressed direction
        if (along <= 0) return;                     // behind us
        var ortho = Math.abs(vx * dy - vy * dx);    // sideways drift
        var score = ortho * 2 - along;
        if (score < bestScore) { bestScore = score; best = n; }
      });
        if (best) { clearPath(); dispatchInteraction({type:"SELECT",id:best.id(),source:"arrow"}); }
      }
      document.addEventListener("keydown", function (e) {
        if (e.defaultPrevented) return;
        if (document.querySelector(".okf-palette-overlay:not([hidden])")) return;
        if (document.querySelector('[role="dialog"]:not([hidden]),[role="menu"]:not([hidden])')) return;
        var graphSurface = e.target === canvasProxy || !!(e.target && e.target.classList && e.target.classList.contains("okf-node-index__item"));
        if (!graphSurface || (isTypingTarget(e.target) && e.target !== canvasProxy && !e.target.classList.contains("okf-node-index__item"))) return;
      if (e.key === "f" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault(); cancelPendingViewportRestore();overlayAwareFit();
      } else if (e.key === "/") {
        var si = document.getElementById("okf-search");
        if (si) { e.preventDefault(); si.focus(); }
      } else if (e.key === "Escape") {
        e.preventDefault();
        clearLocalInteraction();
        canvasProxy.focus();
      } else if (e.key === "Enter") {
        var openId=interactionState.active||interactionState.selected;
        if (e.target===canvasProxy&&openId) { e.preventDefault(); openConceptPage(openId); }
      } else if (e.key === " " && e.shiftKey) {
        if(interactionState.active){e.preventDefault();dispatchInteraction({type:"PATH",id:interactionState.active});}
      } else if (e.key === " ") {
        if(interactionState.active){e.preventDefault();dispatchInteraction({type:"SELECT",id:interactionState.active,source:e.target===canvasProxy?"canvas-keyboard":"index-keyboard"});}
      } else if (e.key === "ArrowUp") { e.preventDefault(); walkSelection(0, -1); }
      else if (e.key === "ArrowDown") { e.preventDefault(); walkSelection(0, 1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); walkSelection(-1, 0); }
      else if (e.key === "ArrowRight") { e.preventDefault(); walkSelection(1, 0); }
    });

    var minimapWrap=null,minimapCanvas=null,viewportRerunButton=null,fullscreenButton=null,fullscreenGeneration=0,fullscreenPending=false;
    function viewportIcon(name){var svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 24 24");svg.setAttribute("aria-hidden","true");svg.classList.add("okf-viewport-icon");var paths={plus:"M12 5v14M5 12h14",minus:"M5 12h14",/* fit: four arrows pointing in at a centre content box (never bare corner brackets: those read as fullscreen) */fit:"M9 9h6v6H9zM3 3l3.5 3.5M6.5 3.5v3h-3M21 3l-3.5 3.5M17.5 3.5v3h3M3 21l3.5-3.5M6.5 20.5v-3h-3M21 21l-3.5-3.5M17.5 20.5v-3h3",/* reset: counter-clockwise circular arrow (rotate-ccw) */reset:"M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8M3 3v5h5",fullscreen:"M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5",exit:"M9 9H4V4M15 9h5V4M9 15H4v5M15 15h5v5",minimap:"M4 5h16v14H4zM8 15l3-4 3 2 3-5",layout:"M12 3a9 9 0 0 1 8 5M20 3v5h-5M12 21a9 9 0 0 1-8-5M4 21v-5h5"};var p=document.createElementNS(svg.namespaceURI,"path");p.setAttribute("d",paths[name]||paths.layout);p.setAttribute("fill","none");p.setAttribute("stroke","currentColor");p.setAttribute("stroke-width","2");p.setAttribute("stroke-linecap","round");p.setAttribute("stroke-linejoin","round");svg.appendChild(p);return svg;}
    function setViewportIcon(button,name){var old=button.querySelector(".okf-viewport-icon"),next=viewportIcon(name);if(old)button.replaceChild(next,old);else button.insertBefore(next,button.firstChild);}
      function syncMinimap(){if(!minimapCanvas||!minimapWrap||minimapWrap.hidden)return;var ctx=minimapCanvas.getContext("2d");if(!ctx)return;var w=minimapCanvas.width,h=minimapCanvas.height,nodes=cy.nodes().not(".dim");ctx.clearRect(0,0,w,h);if(!nodes.length)return;/* Ledger overview: --text-3 dots on the panel, the viewport as a 1.5px ink frame (no tinted wash); forced colours use system ink and Highlight. */var pal=graphPalette(),sys=FORCED_COLORS?systemColours():null,nodeColour=sys?sys.text:pal.muted,frameColour=sys?sys.highlight:pal.cue,bb=nodes.boundingBox(),sx=(w-12)/Math.max(1,bb.w),sy=(h-18)/Math.max(1,bb.h),s=Math.min(sx,sy);ctx.fillStyle=nodeColour;nodes.forEach(function(n){var p=n.position();ctx.beginPath();ctx.arc(6+(p.x-bb.x1)*s,12+(p.y-bb.y1)*s,FORCED_COLORS?3.5:2.5,0,Math.PI*2);ctx.fill();});var ext=cy.extent(),x=6+(ext.x1-bb.x1)*s,y=12+(ext.y1-bb.y1)*s,rw=Math.max(3,ext.w*s),rh=Math.max(3,ext.h*s);ctx.strokeStyle=frameColour;ctx.lineWidth=FORCED_COLORS?3:1.5;ctx.strokeRect(x,y,rw,rh);minimapCanvas.dataset.zoom=String(cy.zoom());minimapCanvas.dataset.nodeCount=String(nodes.length);minimapCanvas.title="Overview map: "+nodes.length+" visible concepts; outlined rectangle is the current viewport.";var label=minimapWrap.querySelector("span");if(label)label.textContent="Overview · "+nodes.length;}
    // ---- Phase 4: zoom cluster (bottom-right) ---------------------------
    (function buildZoomCluster() {
      var wrap = document.createElement("div");
      wrap.className = "okf-graph-zoom";
      wrap.setAttribute("role", "toolbar");
      wrap.setAttribute("aria-label", "Graph view controls");
      function zbtn(icon, title, shortLabel, fn) {
        var b = document.createElement("button");
        b.type = "button";
        b.appendChild(viewportIcon(icon));
        var label=document.createElement("span");label.className="okf-viewport-label";label.textContent=shortLabel;b.appendChild(label);
        b.title = title;
        b.dataset.tooltip = title;
        b.setAttribute("aria-label", title);
        b.addEventListener("click", fn);
        wrap.appendChild(b);
        return b;
      }
      function zoomBy(f) {
        cancelPendingViewportRestore();
        var z = cy.zoom() * f;
        cy.zoom({ level: Math.min(MAX_ZOOM, Math.max(0.06, z)),
                  renderedPosition: { x: container.clientWidth / 2, y: container.clientHeight / 2 } });
      }
      zbtn("plus", "Zoom in", "Zoom in", function () { zoomBy(1.3); });
      zbtn("minus", "Zoom out", "Zoom out", function () { zoomBy(1 / 1.3); });
      zbtn("fit", "Fit graph (f)", "Fit", function () { cancelPendingViewportRestore();overlayAwareFit();syncMinimap(); });
      fullscreenButton=zbtn("fullscreen","Enter fullscreen","Full screen",function(){var own=++fullscreenGeneration;fullscreenPending=true;fullscreenButton.disabled=true;var request=document.fullscreenElement===container?(document.exitFullscreen?document.exitFullscreen():Promise.reject(new Error("Fullscreen unavailable"))):(container.requestFullscreen?container.requestFullscreen():Promise.reject(new Error("Fullscreen unavailable")));Promise.resolve(request).catch(function(){if(own===fullscreenGeneration&&graphPageActive)setStatusNote("Fullscreen request was rejected.");}).finally(function(){if(own!==fullscreenGeneration||!graphPageActive)return;fullscreenPending=false;fullscreenButton.disabled=false;fullscreenButton.focus();});});
      var resetControl=document.getElementById("okf-reset");if(resetControl){resetControl.textContent="";resetControl.appendChild(viewportIcon("reset"));var resetLabel=document.createElement("span");resetLabel.className="okf-viewport-label";resetLabel.textContent="Reset";resetControl.appendChild(resetLabel);resetControl.title="Reset graph view";resetControl.dataset.tooltip="Reset graph view";resetControl.setAttribute("aria-label","Reset graph view");wrap.appendChild(resetControl);}
      var minimapButton=zbtn("minimap","Show minimap","Overview",function(){minimapWrap.hidden=!minimapWrap.hidden;var minimapAction=minimapWrap.hidden?"Show minimap":"Hide minimap";minimapButton.setAttribute("aria-pressed",String(!minimapWrap.hidden));minimapButton.setAttribute("aria-label",minimapAction);minimapButton.title=minimapAction;minimapButton.dataset.tooltip=minimapAction;syncMinimap();});minimapButton.setAttribute("aria-pressed","false");
      viewportRerunButton=zbtn("layout", "Re-run layout", "Layout", function () { viewportRerunButton.disabled=true;viewportRerunButton.dataset.layoutState="running";viewportRerunButton.classList.add("is-running");viewportRerunButton.title="Layout running";viewportRerunButton.dataset.tooltip="Layout running";runLayoutNow(true,false,true); });viewportRerunButton.dataset.layoutState="ready";
      container.appendChild(wrap);
      // The zoom column (bottom-right) and the All concepts summary
      // (top-right) share the right edge. When the canvas is too short for
      // both (the single-file Search & context drawer open, a short window)
      // mark it so CSS lays the cluster out as a bottom-left row instead of
      // stacking it over the summary. Docked mobile toolbars are not direct
      // children and never take the mark.
      function syncCanvasClearance(){var zoom=wrap.parentNode===container?wrap:null,index=container.querySelector(":scope > .okf-node-index"),summary=index&&index.querySelector("summary");var short=false;if(zoom&&summary&&summary.offsetParent!==null){var buttons=Array.prototype.filter.call(zoom.children,function(child){return child.offsetParent!==null;}),column=0;buttons.forEach(function(child){column+=child.offsetHeight;});column+=Math.max(0,buttons.length-1)*4;short=container.clientHeight<index.offsetTop+summary.offsetHeight+8+column+14;}if(short!==container.hasAttribute("data-okf-graph-short"))container.toggleAttribute("data-okf-graph-short",short);}
      if(typeof ResizeObserver!=="undefined"){var canvasClearanceObserver=new ResizeObserver(syncCanvasClearance);canvasClearanceObserver.observe(container);window.addEventListener("pagehide",function(){canvasClearanceObserver.disconnect();});window.addEventListener("pageshow",function(event){if(event.persisted)canvasClearanceObserver.observe(container);});}
      syncCanvasClearance();
      if(isMobileGraphViewport()){var viewportToolbar=document.createElement("div");viewportToolbar.className="okf-graph-viewport-toolbar";container.appendChild(viewportToolbar);viewportToolbar.appendChild(wrap);}
      ["pointerdown","mousedown","touchstart","click","dblclick","wheel"].forEach(function(name){wrap.addEventListener(name,function(e){e.stopPropagation();});});
      minimapWrap=document.createElement("div");minimapWrap.className="okf-graph-minimap";minimapWrap.hidden=true;minimapWrap.setAttribute("aria-hidden","true");var minimapLabel=document.createElement("span");minimapLabel.textContent="Overview";minimapWrap.appendChild(minimapLabel);minimapCanvas=document.createElement("canvas");minimapCanvas.width=160;minimapCanvas.height=100;minimapWrap.appendChild(minimapCanvas);container.appendChild(minimapWrap);
      document.addEventListener("fullscreenchange",function(){if(!graphPageActive)return;fullscreenPending=false;var active=document.fullscreenElement===container;setViewportIcon(fullscreenButton,active?"exit":"fullscreen");fullscreenButton.setAttribute("aria-label",active?"Exit fullscreen":"Enter fullscreen");fullscreenButton.title=active?"Exit fullscreen":"Enter fullscreen";fullscreenButton.dataset.tooltip=fullscreenButton.title;fullscreenButton.disabled=false;if(!active)fullscreenButton.focus();});
      window.addEventListener("pageshow",function(event){if(!event.persisted)return;fullscreenGeneration++;fullscreenPending=false;var active=document.fullscreenElement===container;setViewportIcon(fullscreenButton,active?"exit":"fullscreen");fullscreenButton.disabled=false;fullscreenButton.setAttribute("aria-label",active?"Exit fullscreen":"Enter fullscreen");fullscreenButton.title=active?"Exit fullscreen":"Enter fullscreen";fullscreenButton.dataset.tooltip=fullscreenButton.title;publishGraphCaptureReady("loading");cy.resize();renderInteraction();positionActionBar();syncMinimap();});
      cy.on("pan zoom position",syncMinimap);
      if(GRAPH_EMPTY)Array.prototype.forEach.call(wrap.querySelectorAll("button"),function(button){button.disabled=true;});
    })();

      var resetBtn = document.getElementById("okf-reset");
      if (resetBtn) resetBtn.addEventListener("click", function () {
        // P1 progressive disclosure: reset returns to the documented default
        // (salient subset, complete relationship detail opt-in off).
        controlState.relationshipDetailOptIn = false;
        controlState.showEdgeLabels = false;
        controlState.relationEdges = false;
        if (ui.relLabels) ui.relLabels.checked = false;
        clearPath();exitFocus({restoreFocus:false,quietUrl:true});dispatchInteraction({type:"CLEAR"});
        reconcileVisibleRelationshipProjection();
        overlayAwareFit();
      });

    var searchInput = document.getElementById("okf-search");
    if (searchInput) {
      // Find in graph keeps one row: Clear sits inside the field's end edge
      // and the match status follows the field (outside its label, so it
      // reads the query as typed rather than in the label's eyebrow caps).
      var searchField=searchInput.closest("label")||searchInput.parentNode;searchField.classList.add("okf-graph-search-field");
      var searchStatus=document.createElement("span");searchStatus.className="okf-graph-search-status";searchField.insertAdjacentElement("afterend",searchStatus);
      var clearSearch=document.createElement("button");clearSearch.type="button";clearSearch.className="okf-graph-search-clear";clearSearch.textContent="Clear";clearSearch.hidden=true;searchInput.insertAdjacentElement("afterend",clearSearch);
      var searchTimer=null;
      function commitSearch() {
        searchTimer=null;
        dispatchInteraction({type:"FILTER_SEARCH",value:searchInput.value,quietUrl:true});
        var matches=controlState.search?cy.nodes(".okf-search-match").length:cy.nodes().length,total=cy.nodes().length;searchStatus.textContent=controlState.search?(matches?matches+" of "+total+" concepts match":"No concepts match “"+searchInput.value.trim()+"”"):"";searchStatus.title=searchStatus.textContent;clearSearch.hidden=!controlState.search;
        updateStatus();
        syncMinimap();
      }
      searchInput.addEventListener("input", function(){if(searchTimer)clearTimeout(searchTimer);searchTimer=setTimeout(commitSearch,120);});
      function clearGraphSearch(){if(searchTimer)clearTimeout(searchTimer);searchTimer=null;searchInput.value="";dispatchInteraction({type:"FILTER_SEARCH",value:"",quietUrl:true});searchStatus.textContent="";clearSearch.hidden=true;syncMinimap();}
      clearSearch.addEventListener("click",function(){clearGraphSearch();searchInput.focus();});searchInput.addEventListener("keydown",function(e){if(e.key==="Escape"&&(controlState.search||searchInput.value||searchTimer)){e.preventDefault();clearGraphSearch();canvasProxy.focus();}});
    }

    if (typeSel) typeSel.addEventListener("change", function (e) {
      dispatchInteraction({type:"FILTER_TYPE",value:e.target.value,quietUrl:true});
    });

    // Build the panel + apply the default lens. Initial positions
    // are seeded DETERMINISTICALLY (sorted ids on a circle) before the
    // force layout refines them with randomize:false — the same bundle
    // lays out the same way on every load (the top Obsidian complaint was
    // position churn destroying spatial memory).
    buildSignalPanel();
    if(ui.settingsBody){var layoutGroup=el("fieldset",{class:"okf-signal__group okf-graph-layout-settings"});layoutGroup.appendChild(el("legend",{},["Layout and relationship bundles"]));if(viewportRerunButton)layoutGroup.appendChild(viewportRerunButton);if(bundleIndex)layoutGroup.appendChild(bundleIndex);ui.settingsBody.appendChild(layoutGroup);}
    if (GRAPH_EMPTY) {
      applyVisualEncoding();
      applyFilters();
      updateFocusControls();
      updateStatus();
      setControlsDisabled(true);
      renderLensSummary();
      overlayAwareFit();
      } else {
        (function seedPositions() {
          var ns = cy.nodes().sort(function (a, b) { return a.id() < b.id() ? -1 : 1; });
          var R = Math.max(200, ns.length * 18);
          ns.forEach(function (n, i) {
            var a = (2 * Math.PI * i) / Math.max(1, ns.length);
            n.position({ x: Math.cos(a) * R, y: Math.sin(a) * R });
          });
        })();
        // P1 progressive disclosure: apply backbone elision BEFORE the first
        // real layout so the layout only considers backbone edges (cleaner
        // initial positions — the hairball-free default).
        applyEdgeElision();
        // Lens data, filter/interaction classes and labels form one initial
        // projection. Flush styles before any layout/bounding-box reads.
        cy.batch(function () { applyLens(controlState.lens); });
      }
    var fitDebounced = debounce(function () { overlayAwareFit(); }, 160);
    if (window.addEventListener) window.addEventListener("resize", fitDebounced);
    // The Advanced disclosure lives in the detail pane, so its
    // toggle only affects the canvas fit in the compatibility in-canvas mount.
    if (panelEl && container.contains(panelEl)) {
      var advEl = panelEl.querySelector(".okf-signal__advanced");
      if (advEl) advEl.addEventListener("toggle", function () { fitDebounced(); });
    }

    // ---- Detail-panel relationship explanation -------------------------
    // All figures are read from the SAME precomputed client-side metrics that
    // drive the canvas, so the panel never advertises a signal that is not
    // actually present (contract-runtime-parity for the copy).
    function topSharedTags(id) {
      var d = nodeIndex[id] || {};
      var mine = setOf(d.tags);
      if (!Object.keys(mine).length) return [];
      var counts = {};
      (outgoing[id] || []).concat(backlinks[id] || []).forEach(function (nb) {
        var nd = nodeIndex[nb]; if (!nd) return;
        arr(nd.tags).forEach(function (t) { var k = String(t).toLowerCase(); if (mine[k]) counts[t] = (counts[t] || 0) + 1; });
      });
      return Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; }).slice(0, 3);
    }
    function standoutText(id) {
      var defs = [["links", "link count"], ["backl", "backlinks"], ["typed", "typed relations"], ["tags", "shared tags"], ["cites", "citations"], ["ents", "shared entities"]];
      var best = null, bestV = -1;
      defs.forEach(function (dd) {
        if (!hasSignal[dd[0]]) return;
        var v = normNode(dd[0], id);
        if (v > bestV) { bestV = v; best = dd; }
      });
      var parts = [];
      if (best && bestV >= 0.5) parts.push("top-tier " + best[1] + " (" + Math.round(bestV * 100) + "% of the strongest)");
      else if (best && bestV > 0) parts.push("moderate " + best[1]);
      var gs = {};
      (outgoing[id] || []).concat(backlinks[id] || []).forEach(function (nb) { var g = groupOf[nb]; if (g) gs[g] = true; });
      if (Object.keys(gs).length >= 3) parts.push("bridges " + Object.keys(gs).length + " groups");
      if (!parts.length) return "No standout signal yet";
      var joined = parts.join("; ");
      return joined.charAt(0).toUpperCase() + joined.slice(1);
    }
    function renderSignalExplain(id) {
      var content = document.getElementById("detail-content");
      if (!content) return;
      var box = document.getElementById("okf-detail-signals");
      var dl = box && box.querySelector("dl");
      if (!box || !dl) return;
      while (dl.firstChild) dl.removeChild(dl.firstChild);
      var og = (outgoing[id] || []).length, bl = (backlinks[id] || []).length;
      setText("detail-connection-summary-text", (og + bl) + " total · " + og + " outgoing · " + bl + " incoming");
      var rows = [];
      var tt = topSharedTags(id);
      if (tt.length) rows.push(["Shared tags", tt.join(", ")]);
      if ((raw.typed[id] || 0) > 0) rows.push(["Typed relations", String(raw.typed[id])]);
      if ((raw.cites[id] || 0) > 0) rows.push(["Citations", String(raw.cites[id])]);
      // Phase 3: deterministic broken-link count from Graph.unresolved
      // (server ships as ``unresolved_count``). Surfaces the authoring
      // TODO in the detail panel — a non-colour channel that mirrors the
      // canvas underlay cue (a11y: not colour-only).
      var d = nodeIndex[id] || {};
      var unres = Number(d.unresolved_count) || 0;
      if (unres > 0) rows.push(["Broken links", String(unres)]);
      // Positive active counts only — a "0" row is an empty chip, not a
      // signal (matches the canvas badge and index-card chip rules).
      if (Number(d.active_comment_count) > 0) {
        rows.push(["Active comments", String(d.active_comment_count)]);
      }
      rows.push(["Stands out by", standoutText(id)]);
      rows.forEach(function (r) {
        dl.appendChild(el("dt", {}, [r[0]]));
        dl.appendChild(el("dd", {}, [r[1]]));
      });
      box.hidden = false;
    }

      function clearSelection(options) {
        options=options||{};
        cy.elements().unselect();
        if(!options.preserveFocus)focusRoot = null;
        applyFilters();
        applyEdgeLabels();
        applyEdgeElision();  // P1 progressive disclosure: re-elide non-backbone edges
        updateFocusControls();
        updateStatus();
      var empty = document.getElementById("detail-empty");
      var content = document.getElementById("detail-content");
      if (empty) empty.hidden = false;
      if (content) content.hidden = true;
      hideReservedDocument();
      publishGraphSelection(null, "clear");
    }

      function renderRelationshipProjection() {
        var conceptId=interactionState.selected,slot=document.getElementById("detail-relationships-slot");
        var old=document.querySelector(".okf-detail-relationships");if(old)old.remove();
        if(!slot||!validInteractionId(conceptId))return;
        var node=cy.getElementById(conceptId);if(!node.length)return;
        var edges=node.connectedEdges().not(".dim").sort(function(a,b){return a.id().localeCompare(b.id());});
        var section=document.createElement("section");section.className="okf-detail-relationships";section.setAttribute("aria-labelledby","okf-detail-relationships-title");
        var relationCount=0;edges.forEach(function(edge){relationCount+=visibleEdgeRelations(edge).length;});
        var title=document.createElement("h3");title.id="okf-detail-relationships-title";title.textContent="Relationships ("+relationCount+")";section.appendChild(title);
        var list=document.createElement("ul");list.className="okf-detail-relationships__list";list.setAttribute("role","listbox");list.setAttribute("aria-label","Relationships for selected concept");list.tabIndex=0;
        function humanizeRelation(value){return String(value||"related").replace(/[_-]+/g," ").replace(/\s+/g," ").trim().replace(/^./,function(c){return c.toUpperCase();});}
        var optionIndex=0;edges.forEach(function(edge){var outgoingEdge=edge.source().id()===conceptId,source=edge.source(),target=edge.target(),related=outgoingEdge?target:source,instances=visibleEdgeRelations(edge);instances.forEach(function(instance){var item=document.createElement("li"),relation=instance.type||instance.label||"related";
          item.id="okf-graph-relationship-option-"+(optionIndex++);item.setAttribute("role","option");item.tabIndex=-1;item.setAttribute("aria-selected","false");item.dataset.relatedId=related.id();item.setAttribute("aria-label",humanizeRelation(relation)+", "+(outgoingEdge?"outgoing to ":"incoming from ")+(related.data("label")||related.id()));
          item.dataset.relationId=instance.id||relation;
          [["Type",humanizeRelation(relation),relation],["Label",instance.label||relation],["Source",source.data("label")||source.id()],["Direction",outgoingEdge?"Outgoing":"Incoming"],["Target",target.data("label")||target.id()]].forEach(function(row){item.appendChild(el("span",{class:"okf-detail-relationships__key"},[row[0]]));var value=el("span",{},[row[1]]);if(row[2])value.dataset.rawRelation=row[2];item.appendChild(value);});
          item.dataset.edgeId=edge.id();item.dataset.sourceId=source.id();item.dataset.targetId=target.id();item.dataset.relatedId=related.id();
          item.addEventListener("click",function(){dispatchInteraction({type:"SELECT",id:item.dataset.relatedId,source:"relationship"});});list.appendChild(item);
        });});section.appendChild(list);slot.appendChild(section);
        if(!optionIndex){var none=document.createElement("p");none.className="okf-muted okf-detail-relationships__empty";none.textContent="No visible relationships for this concept.";section.appendChild(none);list.hidden=true;return;}
        function relationshipOptions(){return Array.prototype.slice.call(list.querySelectorAll('[role="option"]'));}
        function activateRelationship(index, scroll){var rows=relationshipOptions();index=Math.max(0,Math.min(rows.length-1,index));var row=rows[index];list.setAttribute("aria-activedescendant",row.id);rows.forEach(function(item){item.classList.toggle("is-active",item===row);});dispatchInteraction({type:"ACTIVE",id:row.dataset.relatedId,quietUrl:true});if(scroll !== false)row.scrollIntoView({block:"nearest"});}
        list.addEventListener("focus",function(){activateRelationship(0);});
        list.addEventListener("keydown",function(event){var rows=relationshipOptions(),current=list.getAttribute("aria-activedescendant"),index=rows.findIndex(function(row){return row.id===current;});if(index<0)index=0;if(event.key==="ArrowDown"||event.key==="ArrowRight"){event.preventDefault();activateRelationship(index+1);}else if(event.key==="ArrowUp"||event.key==="ArrowLeft"){event.preventDefault();activateRelationship(index-1);}else if(event.key==="Home"){event.preventDefault();activateRelationship(0);}else if(event.key==="End"){event.preventDefault();activateRelationship(rows.length-1);}else if(event.key===" "){event.preventDefault();var related=rows[index].dataset.relatedId;if(event.shiftKey)dispatchInteraction({type:"PATH",from:conceptId,id:related});else dispatchInteraction({type:"SELECT",id:related,source:"relationship-keyboard"});}else if(event.key==="Enter"){event.preventDefault();openConceptPage(rows[index].dataset.relatedId);}else if(event.key==="Escape"){event.preventDefault();clearLocalInteraction();}});
        // Initial active-descendant state must not scroll to relationships
        // below the authored body. Only deliberate traversal owns that scroll.
        activateRelationship(0, false);
      }

      function paintDetailMetadata(metadataSlot, html) {
        // Escaped server HTML, never constructed from raw metadata.
        metadataSlot.innerHTML = html || "";
        metadataSlot.querySelectorAll("[data-okf-source-concept]").forEach(function (link) {
          var target = link.dataset.okfSourceConcept;
          var fragment = link.dataset.okfSourceFragment;
          if (!nodeIndex[target]) { link.removeAttribute("href"); return; }
          if (!IS_SINGLE) link.href = conceptPagePrefix.replace(/\/$/, "") + "/" + target + (MODE === "static" ? ".html" : "") + (fragment ? "#" + encodeURIComponent(fragment) : "");
          else link.addEventListener("click", function (event) {
            event.preventDefault(); dispatchInteraction({type:"SELECT",id:target,source:"detail-link"});
            var heading = document.getElementById("detail-title");
            if (fragment) heading = Array.from(document.querySelectorAll("#detail-body [id]")).find(function(el) { return el.id === fragment; }) || heading;
            if (heading) { heading.tabIndex = -1; heading.focus(); }
          });
        });
      }
      function paintDetailBody(bodyEl, html) {
        bodyEl.innerHTML = html;
        bodyEl.removeAttribute("aria-busy");
        rewriteInternalLinks(bodyEl);
        var fragment = bodyEl.dataset.okfPendingFragment;
        delete bodyEl.dataset.okfPendingFragment;
        if (fragment) focusDetailFragment(bodyEl, fragment, document.getElementById("detail-title"));
        // Existing progressive renderers also see lazily loaded bodies.
        try { window.dispatchEvent(new Event("okf-loom:bodyPatched")); } catch (e) {}
      }
      function graphBodyErrorMessage(error) {
        if (error.status === 409) return "This bundle changed while the concept was loading. Try again to load the latest version.";
        if (error.status === 413) return "This concept is too large to display here. Open its page to read it.";
        if (error.status === 503) return "The server is busy or took too long to load this concept. Try again.";
        if (error.name === "AbortError" || error.name === "TimeoutError") return "Loading this concept took too long or was interrupted. Try again.";
        if (error instanceof TypeError) return "Could not reach the server to load this concept. Check your connection and try again.";
        return "This concept could not be loaded. Try again.";
      }
      function showSelectedBody(conceptId, bodyEl, metadataSlot, retriedRevision) {
        if (!bodyEl || !metadataSlot) return;
        if (LIVE_GRAPH && !bundle.bodies) {
          var owner = selectionGeneration, epoch = bodyEpoch;
          var bodyStatus = document.getElementById("detail-body-status");
          if (!bodyStatus) {
            bodyStatus = document.createElement("div"); bodyStatus.id = "detail-body-status";
            bodyStatus.setAttribute("role", "status"); bodyEl.before(bodyStatus);
          }
          bodyStatus.textContent = "";
          function stillSelected() { return graphPageActive && epoch === bodyEpoch && owner === selectionGeneration && selectedConceptId === conceptId; }
          function showBodyError(error) {
            if (!stillSelected()) return;
            detailBodyPending = false; detailBodyError = graphBodyErrorMessage(error);
            bodyEl.removeAttribute("aria-busy"); bodyEl.textContent = "";
            var message = document.createElement("p"); message.textContent = detailBodyError;
            var retry = document.createElement("button"); retry.type = "button";
            retry.className = "okf-btn okf-detail-body-retry"; retry.textContent = "Retry";
            retry.addEventListener("click", function () {
              if (!stillSelected()) return;
              showSelectedBody(conceptId, bodyEl, metadataSlot);
              bodyEl.tabIndex = -1; bodyEl.focus({ preventScroll: true });
            });
            bodyStatus.replaceChildren(message, retry);
          }
          detailBodyPending = true; detailBodyError = null;
          bodyEl.textContent = "Loading concept…"; bodyEl.setAttribute("aria-busy", "true");
          readGraphBody(conceptId).then(function (doc) {
            if (!graphPageActive || epoch !== bodyEpoch || owner !== selectionGeneration || selectedConceptId !== conceptId) return;
            paintDetailMetadata(metadataSlot, doc.metadata_html);
            paintDetailBody(bodyEl, doc.html); detailBodyPending = false;
          }).catch(function (error) {
            if (!stillSelected()) return;
            if (error.status === 409 && !retriedRevision) {
              // Reuse the same latest-wins structure owner even without SSE.
              // Acceptance cancels this epoch and retries the current selection
              // once; a failed refresh leaves this owner to report the error.
              refreshGraphFromServer({ retryBodyRevision: true }).then(function (accepted) {
                if (!accepted) showBodyError(error);
              });
              return;
            }
            showBodyError(error);
          });
        } else paintDetailBody(bodyEl, bodies[conceptId] || "");
      }
      function showDetail(conceptId, provenance) {
      var data = nodeIndex[conceptId];
      if (!data) return;
      // iter2 CRI2-006: decluster-on-focus. A keyboard-index or tap on a
      // node the LOD hid must still resolve: add it to the canvas first.
      ensureNodeVisible(conceptId);
      cy.elements().unselect();
      var node = cy.getElementById(conceptId);
      if (node && node.length) node.select();
      publishGraphSelection(conceptId, "detail");
      syncInteractionIndex();
      updateStatus();
        var keyboardActivation = provenance === "keyboard" || provenance === "index-keyboard" || provenance === "public-keyboard";
          var mobileKeyboard = keyboardActivation && isCompactGraphViewport();
          if (isCompactGraphViewport()) setMobilePane("details", false);

        // Show edge labels for the selected node's edges (context without
        // global clutter — reviewer #8).
        applyEdgeLabels();
        // P1 progressive disclosure: reveal all incident edges of the selected
        // node (they get the .okf-incident highlight via applyEdgeLabels).
        applyEdgeElision();
      // Selected-neighbourhood focus tracks the selected node: re-run the
      // combined filter so the focus dim follows the new selection, then zoom
      // the neighbourhood into view so Focus visibly isolates it (reviewer #4).
      if (controlState.focusEnabled) {
        applyFilters();
        updateFocusControls();
        updateStatus();
        overlayAwareFit();
      }

      var empty = document.getElementById("detail-empty");
      var content = document.getElementById("detail-content");
      if (empty) empty.hidden = true;
      if (content) content.hidden = false;
      hideReservedDocument();

      renderRelationshipProjection();

      var chip = document.getElementById("detail-type");
      if (chip) {
        // An outlined mono type badge (graph.css); the type name carries the
        // distinction, never a fill colour.
        chip.textContent = data.type || "concept";
        chip.style.removeProperty("background");
        chip.style.removeProperty("color");
      }
      setText("detail-title", data.label || conceptId);
      var selectedStatus=document.querySelector(".okf-detail-selected-status");if(selectedStatus)selectedStatus.textContent="Selected: "+(data.label||conceptId);
      setText("detail-id", conceptId);
      setText("detail-description", data.description || "Not set");

      // Resource.
      var resourceEl = document.getElementById("detail-resource");
      if (resourceEl) {
        resourceEl.innerHTML = "";
        if (data.resource) {
          var a = document.createElement("a");
          a.href = data.resource; a.textContent = data.resource;
          a.target = "_blank"; a.rel = "noopener"; a.className = "okf-external";
          resourceEl.appendChild(a);
        } else {
          // iter1 CRI-009: em dash replaced with a screen-reader-friendly
          // "Not set" so an empty field reads as content, not a glyph.
          resourceEl.textContent = "Not set";
        }
      }

      // Tags.
      var tagsEl = document.getElementById("detail-tags");
      if (tagsEl) {
        tagsEl.innerHTML = "";
        if (data.tags && data.tags.length) {
          data.tags.forEach(function (t) {
            var span = document.createElement("span");
            span.className = "okf-tag"; span.textContent = t;
            tagsEl.appendChild(span);
          });
        } else {
          tagsEl.textContent = "Not set";
        }
      }

      // §7 governed keys (P1-3 iter-1): aliases / entities / provenance /
      // citations / relations - mirrors the concept page. Hidden when empty.
      renderGoverned(document.getElementById("detail-governed"), data);
      var metadataSlot = document.getElementById("detail-okf-metadata");
      if (!metadataSlot) {
        metadataSlot = document.createElement("div");
        metadataSlot.id = "detail-okf-metadata";
        var detailDescription = document.getElementById("detail-description");
        if (detailDescription) detailDescription.after(metadataSlot);
      }
      paintDetailMetadata(metadataSlot, data.metadata_html || "");

      // Computed relationship explanation — why this node connects and
      // stands out. Every figure comes from the SAME client-side metrics that
      // drive the canvas, so the copy never claims a signal that is not real.
      renderSignalExplain(conceptId);

      // Body: pre-rendered server-side by okf-loom's HTML-escaping
      // markdown renderer (see build_graph_data). Assign directly - no
      // client-side markdown parser is loaded, which removes the entire
      // client-side XSS surface (no marked.parse, no DOMPurify needed).
      var bodyEl = document.getElementById("detail-body");
      if (bodyEl) {
        bodyEl.innerHTML = "";
        delete bodyEl.dataset.okfPendingFragment;
        // Stamp the concept id so studio.js's comment affordance can
        // anchor comments to the right concept when the user selects
        // text in the graph detail panel.
        bodyEl.setAttribute("data-concept-id", conceptId);
        bodyEl.className = "okf-page__body okf-graph-detail__body";
        showSelectedBody(conceptId, bodyEl, metadataSlot);
      }

      // Open-page link (graph view only).
      // P1-3: in static mode the concept page is emitted at <id>.html, so
      // the href needs the .html suffix (the bare path 404s).
      var openLink = document.getElementById("detail-open-link");
      if (openLink) {
        var openSuffix = (MODE === "static") ? ".html" : "";
        openLink.href = conceptPagePrefix.replace(/\/$/, "") + "/" + conceptId + openSuffix;
      }

      // Outgoing.
      renderLinkList("outgoing-list", "detail-outgoing", outgoing[conceptId] || []);
      // Backlinks.
      renderLinkList("backlinks-list", "detail-backlinks", backlinks[conceptId] || []);

      // Pan to node. P2-75: skip animation when user prefers reduced motion.
      // Skip entirely under focus — overlayAwareFit() already framed the whole
      // neighbourhood, and a competing pan-to would undo that isolation.
        // Pointer selection must preserve the camera so a visible adjacent
        // endpoint remains under the pointer for Shift-click path tracing.
        // Keyboard/index activation may still centre the active concept.
        if (node && node.length && !controlState.focusEnabled && keyboardActivation) {
          var lvl = Math.min(Math.max(cy.zoom(), 0.9), MAX_ZOOM);
        if (REDUCED_MOTION) {
          cy.center(node);
          cy.zoom({ eles: node, level: lvl });
        } else {
            cy.animate({ center: { eles: node }, zoom: lvl }, { duration: 200 });
          }
        }
          if (mobileKeyboard || isCompactGraphViewport()) {
          window.requestAnimationFrame(function () {
            var activeOwner = document.getElementById("okf-detail");
            if (activeOwner) activeOwner.scrollTop = 0;
          });
        }
        if (keyboardActivation && content) {
          content.setAttribute("tabindex", "-1");
          window.requestAnimationFrame(function () {
            if (!content.hidden && content.isConnected) {
              /* On mobile the Details pane is the scroll owner.  Letting the
               * browser scroll a newly focused detail region can align it
               * above that pane's top edge, underneath the compact header and
               * tabs.  Start each keyboard-opened concept at the pane top and
               * move focus without a second implicit scroll. */
              if (mobileKeyboard) {
                var detailPane = document.getElementById("okf-detail");
                if (detailPane) detailPane.scrollTop = 0;
              }
              content.focus({ preventScroll: mobileKeyboard });
            }
          }, 0);
        }
      }

      window.okfActivateGraphConcept = function (conceptId, provenance, section) {
        if (!nodeIndex[conceptId]) return false;
        window.dispatchEvent(new CustomEvent("okf-loom:single-destination", {
          detail: { destination: "graph", provenance: provenance || "public" }
        }));
        // R1/R2: a matching-section destination is ONE history entry whose
        // URL is owned by the existing selected state (?selected=<id>#<fragment>)
        // with the search panel snapshot recorded on the PREVIOUS entry by the
        // panel owner before the push. The artifact binding is checked first —
        // against the snapshot's independent rendered-source map (canonical
        // concept identity -> per-concept normalized-source revision), NOT a
        // fallback that treats a missing revision as pass: section metadata
        // that does not belong to this embedded snapshot degrades to ordinary
        // root selection, never a fragment jump.
        var bound = section && typeof section === "object" && section.fragment
          && singleSourceRevisions !== null
          && Object.prototype.hasOwnProperty.call(singleSourceRevisions, conceptId)
          && singleSourceRevisions[conceptId] === section.sourceRevision;
        if (bound) {
          try {
            // The section entry must NOT inherit the search panel snapshot:
            // Forward back to this destination means the panel was closed to
            // open it, so its state is the section state without the
            // previous entry's okfSingleSearch payload, plus the owned
            // section stamp that keeps re-admission strict on traversal.
            var sectionState = Object.assign({}, history.state);
            delete sectionState.okfSingleSearch;
            sectionState.okfSectionIntent = {
              fragment: String(section.fragment),
              revision: section.sourceRevision == null ? null : String(section.sourceRevision),
              heading: section.heading == null ? null : String(section.heading)
            };
            history.pushState(sectionState, "", "?selected="
              + encodeURIComponent(conceptId) + "#"
              + encodeURIComponent(section.fragment));
          } catch (error) { bound = false; }
        }
        dispatchInteraction({type:"SELECT",id:conceptId,source:provenance === "keyboard" ? "public-keyboard" : "public-pointer",quietUrl:bound});
        // Consume a section destination only AFTER the selected document's
        // body is actually displayed (showDetail rendered it synchronously
        // above). Admission re-checks identity inside the displayed body and
        // is deferred (generation-guarded) so closing the search overlay —
        // which returns focus to the toggle — cannot be stolen by a stale
        // async focus when the selection is superseded.
        if (bound) {
          var sectionBody = document.getElementById("detail-body");
          if (sectionBody) {
            focusAdmittedDetailFragment(sectionBody, section.fragment, section.heading == null ? null : String(section.heading));
          }
        } else if (section && typeof section === "object" && section.fragment) {
          // Unbound or invalid metadata: keep the ordinary selection, make
          // sure no stale fragment stays published.
          stripDetailHash();
        }
        return true;
      };

      function renderLinkList(listId, sectionId, ids) {
      var list = document.getElementById(listId);
      var section = document.getElementById(sectionId);
      if (!list || !section) return;
      list.innerHTML = "";
      if (!ids.length) { section.hidden = true; return; }
      section.hidden = false;
      ids.forEach(function (src) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.textContent = (nodeIndex[src] && nodeIndex[src].label) || src;
        a.href = "#node-" + src;
        a.className = "okf-internal";
        a.addEventListener("click", function (e) {
          e.preventDefault(); dispatchInteraction({type:"SELECT",id:src,source:"detail-link"});
        });
        li.appendChild(a);
        var muted = document.createElement("span");
        muted.className = "okf-muted";
        muted.textContent = " " + src;
        li.appendChild(muted);
        list.appendChild(li);
      });
    }

    // P1-4 / P2-2 (iter-1): build the keyboard-accessible node index.
    // One <button> per node, sorted by label. Click / Enter / Space →
    // showDetail(id). Reuses the concept-page local-graph pill pattern.
    function buildNodeIndex(bundleData) {
      var list = document.getElementById("okf-node-index-list");
      var countEl = document.getElementById("okf-node-index-count");
      if (!list) return;
      while (list.firstChild) list.removeChild(list.firstChild);
      list.setAttribute("role", "listbox");
      list.setAttribute("aria-label", "Graph concepts");
      list.setAttribute("tabindex", "0");
      var sorted = (bundleData.nodes || []).slice().sort(function (a, b) {
        var la = String(a.data.label || a.data.id || "").toLowerCase();
        var lb = String(b.data.label || b.data.id || "").toLowerCase();
        if (la < lb) return -1;
        if (la > lb) return 1;
        return 0;
      });
      if (countEl) countEl.textContent = "(" + sorted.length + ")";
      sorted.forEach(function (n, optionIndex) {
        var id = n.data.id;
        var label = n.data.label || id;
        var btn = document.createElement("div");
        btn.className = "okf-node-index__item";
        btn.id = "okf-graph-concept-option-" + optionIndex;
        btn.setAttribute("role", "option");
        btn.setAttribute("tabindex", "-1");
        btn.setAttribute("aria-selected", "false");
        btn.setAttribute("data-target", id);
        btn.setAttribute("aria-controls", "detail-content");
        btn.addEventListener("focus",function(){dispatchInteraction({type:"ACTIVE",id:id,quietUrl:true});});
        // Phase 3: include the deterministic broken-link count in the
        // keyboard-accessible node index's aria-label so assistive tech
        // announces it ("Focus Orders — 2 broken links"). Non-colour
        // channel for the canvas broken-link underlay cue.
        var unres = Number(n.data.unresolved_count) || 0;
        var hasComments = Object.prototype.hasOwnProperty.call(n.data, "active_comment_count");
        var comments = hasComments ? n.data.active_comment_count : null;
        // The VISIBLE chip is a positive-count signal only (mirrors the
        // canvas badge and the detail signals row): an explicit 0 is the
        // absence of a signal, not a "0" chip. The aria phrase keeps the
        // exact count (including 0) — it is the assistive channel for the
        // same fact, not a badge.
        var showComments = hasComments && comments > 0;
        var phrases = [];
        if (unres > 0) phrases.push(unres + " broken link" + (unres === 1 ? "" : "s"));
        if (hasComments) phrases.push(commentPhrase(comments));
        var aria = label + (phrases.length ? " (" + phrases.join(", ") + ")" : "");
        btn.setAttribute("aria-label", aria);
        var sw = document.createElement("span");
        sw.className = "okf-node-index__swatch";
        // Neutral until a colour lens repaints it (updateNodeIndexSwatches);
        // the bundle's type colour is never shown.
        sw.style.background = graphPalette().node;
        btn.appendChild(sw);
        var lbl = document.createElement("span");
        lbl.className = "okf-node-index__label";
          lbl.textContent = label;
          btn.appendChild(lbl);
        // Phase 3: non-colour "!" prefix marks concepts with broken
        // outgoing links in the keyboard node index (mirrors the canvas
        // red underlay without relying on colour alone).
        if (unres > 0) {
          var warn = document.createElement("span");
          warn.className = "okf-node-index__warn";
          warn.setAttribute("aria-hidden", "true");
          warn.textContent = "!";
          btn.appendChild(warn);
        }
        if (showComments) {
          var commentBadge = document.createElement("span");
          commentBadge.className = "okf-node-index__comments";
          commentBadge.setAttribute("aria-hidden", "true");
          commentBadge.textContent = commentCountText(comments);
          commentBadge.title = commentPhrase(comments);
          btn.appendChild(commentBadge);
        }
          btn.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") btn.dataset.keyboardActivation = "1"; });
            btn.addEventListener("click", function (e) {
              e.preventDefault();
              var keyboard = e.detail === 0 || btn.dataset.keyboardActivation === "1";
              delete btn.dataset.keyboardActivation;
              dispatchInteraction({type:"SELECT",id:id,source:keyboard?"index-keyboard":"index-pointer"});
            });
        list.appendChild(btn);
      });
      function options(){return Array.prototype.slice.call(list.querySelectorAll('[role="option"]'));}
      function activateAt(index, moveDomFocus){var rows=options();if(!rows.length){list.removeAttribute("aria-activedescendant");return;}index=Math.max(0,Math.min(rows.length-1,index));var row=rows[index],id=row.dataset.target;dispatchInteraction({type:"ACTIVE",id:id,quietUrl:true});list.setAttribute("aria-activedescendant",row.id);rows.forEach(function(item){item.classList.toggle("is-active",item===row);});row.scrollIntoView({block:"nearest"});if(moveDomFocus)row.focus();}
      list.onfocus=function(){var rows=options(),index=rows.findIndex(function(row){return row.dataset.target===interactionState.active;});activateAt(index>=0?index:0,false);};
      list.onkeydown=function(event){var rows=options();if(!rows.length)return;var current=list.getAttribute("aria-activedescendant"),index=rows.findIndex(function(row){return row.id===current;});if(index<0)index=0;if(event.key==="ArrowDown"||event.key==="ArrowRight"){event.preventDefault();event.stopPropagation();activateAt(index+1,false);}else if(event.key==="ArrowUp"||event.key==="ArrowLeft"){event.preventDefault();event.stopPropagation();activateAt(index-1,false);}else if(event.key==="Home"){event.preventDefault();event.stopPropagation();activateAt(0,false);}else if(event.key==="End"){event.preventDefault();event.stopPropagation();activateAt(rows.length-1,false);}else if(event.key===" "){event.preventDefault();event.stopPropagation();var id=rows[index].dataset.target;if(event.shiftKey)dispatchInteraction({type:"PATH",id:id});else dispatchInteraction({type:"SELECT",id:id,source:"index-keyboard"});}else if(event.key==="Enter"){event.preventDefault();event.stopPropagation();openConceptPage(rows[index].dataset.target);}else if(event.key==="Escape"){event.preventDefault();event.stopPropagation();clearLocalInteraction();list.focus();}}
      if(sorted.length){var initial=list.querySelector('[role="option"]');list.setAttribute("aria-activedescendant",initial.id);initial.classList.add("is-active");}
    }

    // Body and reserved-document hrefs arrive as canonical identities,
    // already resolved server-side from the SELECTED document's directory
    // (sibling, ./, ../): "/path.md" plus an optional "#fragment". This
    // client never re-resolves relative forms; it only translates a canonical
    // identity into the destination that exists in this output.
    function canonicalLink(href) {
      var hash = href.indexOf("#");
      var path = hash < 0 ? href : href.slice(0, hash);
      var fragment = hash < 0 ? "" : href.slice(hash + 1);
      if (path.charAt(0) !== "/" || path.charAt(1) === "/" || path.slice(-3) !== ".md") return null;
      try { fragment = decodeURIComponent(fragment); } catch (e) {}
      return { document: path.slice(1), fragment: fragment };
    }
    function isLogDocument(doc) { return /(^|\/)log\.md$/.test(doc); }
    // Real page for a reserved document in live / spa / static output.
    function reservedDocumentHref(doc, fragment) {
      var root = conceptPagePrefix.replace(/\/$/, "");
      var dest;
      if (MODE === "static") dest = doc.slice(0, -3) + ".html";
      else if (!isLogDocument(doc)) dest = doc.slice(0, -"index.md".length);
      else dest = MODE === "spa" ? doc.slice(0, -3) + ".html" : doc;
      return root + "/" + dest + (fragment ? "#" + encodeURIComponent(fragment) : "");
    }
    // R1/R2 destination admission. A section destination is re-admitted
    // against the ACTUALLY DISPLAYED document: exactly one element with that
    // id in the whole displayed page, it must be a real H1-H6 heading inside
    // the displayed body, and (when the search-side transport proved a
    // renderer-owned label) its text must still equal that label. Anything
    // else — missing, chrome, duplicate, other-document, plugin-renamed —
    // fails closed to document-root reading and the unsafe fragment is
    // cleared WITHOUT a history push.
    var detailAdmissionGeneration = 0;
    function normalizeHeadingLabel(heading) {
      var copy = heading.cloneNode(true);
      Array.prototype.forEach.call(copy.querySelectorAll(".okf-heading-anchor"), function (anchor) { anchor.remove(); });
      return (copy.textContent || "").replace(/\s+/g, " ").trim();
    }
    function detailFragmentUnsafe(fragment) {
      return typeof fragment !== "string" || !fragment || fragment.length > 512 || /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(fragment);
    }
    function admitDetailFragment(body, fragment, expectedLabel) {
      if (!body || detailFragmentUnsafe(fragment)) return null;
      if (typeof CSS === "undefined" || !CSS.escape) return null;
      var selector = '[id="' + CSS.escape(fragment) + '"]';
      var inBody = body.querySelectorAll(selector);
      if (inBody.length !== 1) return null;
      var target = inBody[0];
      // Uniqueness is document-wide: a second element with the same id
      // anywhere (chrome, another pane) makes the destination unaddressable.
      if (document.querySelectorAll(selector).length !== 1) return null;
      if (!/^[Hh][1-6]$/.test(target.tagName)) return null;
      if (target.closest("[hidden]")) return null;
      if (expectedLabel != null && normalizeHeadingLabel(target) !== expectedLabel) return null;
      return target;
    }
    function stripDetailHash() {
      if (!location.hash) return;
      try { history.replaceState(history.state, "", location.pathname + location.search); } catch (error) { /* keep hash: strictly worse to throw */ }
    }
    function focusAdmittedDetailFragment(body, fragment, expectedLabel) {
      var own = ++detailAdmissionGeneration;
      // Bind the queued focus to the destination it was scheduled for: an
      // ordinary superseding selection re-renders the same body element and
      // rewrites the URL WITHOUT bumping the admission generation, so the
      // queued callback must independently refuse a destination that is no
      // longer current (and then never focus, never strip).
      var bodyStamp = body ? (body.getAttribute("data-concept-id") || "") : null;
      var urlStamp = location.pathname + location.search;
      window.requestAnimationFrame(function () {
        if (own !== detailAdmissionGeneration) return; // superseded selection
        if (!body || !body.isConnected) return;
        if (bodyStamp !== null
            && (body.getAttribute("data-concept-id") || "") !== bodyStamp) return;
        if (location.pathname + location.search !== urlStamp) return;
        var target = admitDetailFragment(body, fragment, expectedLabel);
        if (!target) { stripDetailHash(); return; }
        target.tabIndex = -1;
        target.focus({ preventScroll: true });
        target.scrollIntoView({ block: "start", behavior: REDUCED_MOTION ? "auto" : "smooth" });
      });
    }
    function focusDetailFragment(scope, fragment, fallback) {
      if (fragment && scope && scope.getAttribute("aria-busy") === "true") {
        scope.dataset.okfPendingFragment = fragment;
        return;
      }
      var target = fallback;
      if (fragment && scope) {
        target = Array.prototype.slice.call(scope.querySelectorAll("[id]")).filter(function (node) {
          return node.id === fragment;
        })[0] || fallback;
      }
      if (!target) return;
      if (!target.hasAttribute("tabindex")) target.tabIndex = -1;
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: "start", behavior: REDUCED_MOTION ? "auto" : "smooth" });
    }
    function plainActivation(event) {
      return !event.defaultPrevented && event.button === 0 &&
        !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
    }

    // Single-file output has no other page to navigate to, so a reserved
    // document is read in the detail pane. It is NOT a selection: the graph
    // selection is cleared and no node, halo or URL state is fabricated.
    var reservedReturnId = null;
    function hideReservedDocument() {
      var pane = document.getElementById("detail-document");
      if (pane) pane.hidden = true;
    }
    function showReservedDocument(doc, fragment) {
      var record = reservedDocuments[doc];
      var host = document.getElementById("okf-detail");
      var content = document.getElementById("detail-content");
      if (!record || !host || !content) return false;
      // Following a link from one reserved document to another keeps the
      // concept the reader came from as the way back.
      var open = document.getElementById("detail-document");
      var returnId = interactionState.selected || (open && !open.hidden ? reservedReturnId : null);
      reservedReturnId = returnId;
      var returnLabel = returnId && nodeIndex[returnId] ? (nodeIndex[returnId].label || returnId) : "";
      dispatchInteraction({type:"CLEAR"});
      var pane = document.getElementById("detail-document");
      if (!pane) {
        pane = document.createElement("article");
        pane.id = "detail-document";
        pane.className = "okf-detail-document";
        pane.setAttribute("aria-labelledby", "detail-document-title");
        content.after(pane);
      }
      pane.textContent = "";
      var header = el("header", {class:"okf-detail__header okf-detail-document__header"}, [
        el("span", {class:"okf-detail-document__kind"}, [isLogDocument(doc) ? "Update log" : "Index"]),
        el("h2", {id:"detail-document-title", tabindex:"-1"}, [doc]),
        el("p", {class:"okf-muted okf-detail-document__note"}, ["Reserved document. It is not a graph node, so nothing is selected."])
      ]);
      var back = el("button", {type:"button", class:"okf-btn okf-btn--quiet okf-detail-document__back"},
        [returnLabel ? "Back to " + returnLabel : "Close document"]);
      back.addEventListener("click", function () {
        hideReservedDocument();
        if (returnId && nodeIndex[returnId]) {
          dispatchInteraction({type:"SELECT",id:returnId,source:"detail-link"});
          focusDetailFragment(null, "", document.getElementById("detail-title"));
        } else {
          var empty = document.getElementById("detail-empty");
          if (empty) { empty.hidden = false; empty.tabIndex = -1; empty.focus(); }
        }
      });
      header.appendChild(back);
      var body = document.createElement("div");
      body.id = "detail-document-body";
      body.className = "okf-prose okf-page__body okf-graph-detail__body";
      body.setAttribute("data-okf-document", record.document || doc);
      // Escaped and rendered server-side, exactly like concept bodies.
      body.innerHTML = record.html || "";
      pane.appendChild(header);
      pane.appendChild(body);
      rewriteInternalLinks(body);
      var empty = document.getElementById("detail-empty");
      if (empty) empty.hidden = true;
      pane.hidden = false;
      if (isCompactGraphViewport()) setMobilePane("details", false);
      try { window.dispatchEvent(new Event("okf-loom:bodyPatched")); } catch (e) {}
      focusDetailFragment(body, fragment, document.getElementById("detail-document-title"));
      return true;
    }

    function rewriteInternalLinks(root) {
      if (!root) return;
      var links = root.querySelectorAll("a[href]");
      Array.prototype.forEach.call(links, function (a) {
        var href = a.getAttribute("href") || "";
        // Claim-source and ordinary footnotes belong to this document, not
        // a new viewer tab. Keep native fragment focus/history semantics.
        if (href.charAt(0) === "#") {
          a.removeAttribute("target");
          return;
        }
        var link = canonicalLink(href);
        if (link) {
          var conceptId = link.document.slice(0, -3);
          a.removeAttribute("target");
          // A link to a fragment of the concept already on screen (claim
          // source footnotes arrive as canonical /self.md#id hrefs) is a
          // same-page jump. Re-selecting the concept re-rendered the pane and
          // dropped the fragment, so its target never opened or took focus.
          var reserved = root.getAttribute("data-okf-document") || "";
          if (link.fragment && (link.document === reserved ||
              (!reserved && conceptId === root.getAttribute("data-concept-id")))) {
            a.setAttribute("href", "#" + link.fragment);
            if (!a.className) a.className = "okf-internal";
            return;
          }
          if (nodeIndex[conceptId]) {
            // A concept: read it here. The fragment lands on the real heading
            // of the newly shown body, or on the title when it has none.
            a.setAttribute("href", "#node-" + conceptId);
            a.className = "okf-internal";
            a.dataset.okfLinkConcept = conceptId;
            if (link.fragment) a.dataset.okfLinkFragment = link.fragment;
            a.addEventListener("click", function (e) {
              if (!plainActivation(e)) return;
              e.preventDefault();
              dispatchInteraction({type:"SELECT",id:conceptId,source:"detail-link"});
              focusDetailFragment(document.getElementById("detail-body"), link.fragment, document.getElementById("detail-title"));
            });
            return;
          }
          if (reservedDocuments[link.document]) {
            a.className = "okf-internal";
            a.dataset.okfLinkDocument = link.document;
            if (!IS_SINGLE) {
              a.setAttribute("href", reservedDocumentHref(link.document, link.fragment));
              return;
            }
            a.setAttribute("href", "#detail-document");
            a.addEventListener("click", function (e) {
              if (!plainActivation(e)) return;
              e.preventDefault();
              showReservedDocument(link.document, link.fragment);
            });
            return;
          }
          // A bundle-internal identity with no page in this output. Say so;
          // never present it as an external site or a dead new tab.
          a.removeAttribute("href");
          a.setAttribute("data-okf-broken", "1");
          a.setAttribute("title", "Unresolved link: " + link.document + " is not in this bundle.");
          return;
        }
        a.className = "okf-external";
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener");
      });
    }

    // iter1 CRI-004: subscribe to presence so the agent's focus shows up as
    // a halo without taking ownership of graph placement or camera. When presence goes idle (no
    // focus), every halo clears so a stale cue never lingers. This makes
    // the agent feel spatially present in the graph, not just a badge.
    var presenceFocus = null;
    function applyPresenceFocus(focus) {
      cy.nodes().removeClass("okf-presence-halo");
      presenceFocus = focus || null;
      if (!focus) return;
      var node = cy.getElementById(focus);
      if (!node || !node.length || !node.visible()) return;
      node.addClass("okf-presence-halo");
    }
    var ghostLayer = document.createElement("div");
    ghostLayer.className = "okf-presence-ghosts";
    ghostLayer.setAttribute("aria-hidden", "true");
    var livePresenceHub = window.okfLoomLive && window.okfLoomLive.presenceAvailable !== false && typeof window.okfLoomLive.on === "function";
    if (livePresenceHub) graphEl.appendChild(ghostLayer);
    var collaboratorRegion = document.createElement("section");
    collaboratorRegion.className = "okf-collaborator-region";
    collaboratorRegion.setAttribute("aria-label", "Collaborators");
    var collaboratorCompact = document.createElement("p");
    collaboratorCompact.className = "okf-collaborators__compact";
    var collaboratorSection = document.createElement("details");
    collaboratorSection.className = "okf-collaborators";
    collaboratorSection.id = "okf-collaborators-detail";
    var collaboratorTitle = document.createElement("summary");
    collaboratorTitle.className = "okf-collaborators__title";
    collaboratorTitle.textContent = "Collaborators (0)";
    var collaboratorList = document.createElement("ul");
    collaboratorList.className = "okf-collaborators__list";
    collaboratorList.setAttribute("aria-live", "off");
    collaboratorSection.appendChild(collaboratorTitle);
    collaboratorSection.appendChild(collaboratorList);
    collaboratorRegion.appendChild(collaboratorSection);
    // The disclosure summary is the section heading: it names the list and
    // its count, never a collaborator. The compact line under it previews the
    // first collaborator while the list is closed and steps aside while it is
    // open, so no collaborator is named twice and the heading never moves.
    collaboratorRegion.appendChild(collaboratorCompact);
    collaboratorSection.addEventListener("toggle", function () { collaboratorCompact.hidden = collaboratorSection.open; });
    var detailPane = document.getElementById("okf-detail");
    if (detailPane && livePresenceHub) detailPane.appendChild(collaboratorRegion);
    var mobilePresenceBar = document.createElement("aside");
    mobilePresenceBar.className = "okf-mobile-presence";
    mobilePresenceBar.setAttribute("aria-label", "Current collaborators");
    var mobilePresenceText = document.createElement("span"); mobilePresenceText.className = "okf-mobile-presence__text";
    var mobilePresenceOpen = document.createElement("button"); mobilePresenceOpen.type = "button"; mobilePresenceOpen.textContent = "Details"; mobilePresenceOpen.setAttribute("aria-label", "Show collaborator details"); mobilePresenceOpen.setAttribute("aria-controls", collaboratorSection.id);
    mobilePresenceOpen.addEventListener("click", function () { collaboratorSection.open = true; var detailsTab=document.querySelector('[data-graph-pane="details"]'); if(detailsTab)detailsTab.click(); });
    mobilePresenceBar.appendChild(mobilePresenceText); mobilePresenceBar.appendChild(mobilePresenceOpen);
    if (paneNav && paneNav.parentNode && livePresenceHub) paneNav.parentNode.insertBefore(mobilePresenceBar, paneNav.nextSibling);
    var ghostNodes = Object.create(null);
    var overflowGhost = null;
    var lastPresenceSnapshot = null, presenceRenderFrame = null;
    function ghostPosition(row) {
      if (row.viewport) return { x: row.viewport.x, y: row.viewport.y };
      if (row.focus) {
        var focused = cy.getElementById(row.focus);
        if (focused && focused.length && focused.visible()) {
          var pos = focused.renderedPosition();
          return { x: pos.x / Math.max(1, graphEl.clientWidth), y: pos.y / Math.max(1, graphEl.clientHeight) };
        }
      }
      return null;
    }
    function renderPresenceSnapshot(snapshot) {
      lastPresenceSnapshot = snapshot;
      var rows = snapshot && snapshot.version === 2 && snapshot.online !== false && Array.isArray(snapshot.records) ? snapshot.records : [];
      var own = snapshot && snapshot.own_session_id;
      var remote = rows.filter(function (row) { return row && row.session_id !== own; }).sort(function (a,b) { return a.session_id.localeCompare(b.session_id); });
      var summaryRow = remote.find(function(row){return row.kind === "browser";}) || remote[0];
      var agentRow = remote.find(function(row){return row.kind === "agent";});
      var browserCount = remote.filter(function(row){return row.kind === "browser";}).length;
      collaboratorTitle.textContent = "Collaborators (" + remote.length + ")";
      collaboratorCompact.textContent = "No other collaborators here.";
      function setMobileSummary(agentText, collaboratorText){mobilePresenceText.textContent="";var line1=document.createElement("span");line1.className="okf-mobile-presence__agent";line1.textContent=agentText;var line2=document.createElement("span");line2.className="okf-mobile-presence__collaborators";line2.textContent=collaboratorText;mobilePresenceText.appendChild(line1);mobilePresenceText.appendChild(line2);}
      setMobileSummary("Agent idle", "0 collaborators");
      collaboratorList.textContent = "";
      if (!remote.length) {
        var empty = document.createElement("li"); empty.className = "okf-collaborators__empty"; empty.textContent = "No other collaborators here."; collaboratorList.appendChild(empty);
      }
      var seen = Object.create(null);
      var graphRect = graphEl.getBoundingClientRect(), graphWidth = Math.max(1, graphEl.clientWidth), graphHeight = Math.max(1, graphEl.clientHeight);
      var placedBoxes = [], overflowCount = 0, clearance = 8;
      var exclusionSelectors = [".okf-graph-zoom",".okf-node-index",".okf-graph-legend",".okf-graph-actions",".okf-graph-tip",".okf-graph-focusbar",".okf-graph-minimap",".okf-graph-updated",".okf-graph-tour",".okf-signal"];
      exclusionSelectors.forEach(function (selector) { var node = graphEl.querySelector(selector) || document.querySelector(selector); if (!node || node.hidden || node.offsetParent === null) return; var r = node.getBoundingClientRect(); if (r.right <= graphRect.left || r.left >= graphRect.right || r.bottom <= graphRect.top || r.top >= graphRect.bottom) return; placedBoxes.push({left:Math.max(0,r.left-graphRect.left-clearance),top:Math.max(0,r.top-graphRect.top-clearance),right:Math.min(graphWidth,r.right-graphRect.left+clearance),bottom:Math.min(graphHeight,r.bottom-graphRect.top+clearance),reserved:true}); });
      cy.nodes(":visible").forEach(function(node){var r=node.renderedBoundingBox({includeLabels:true,includeOverlays:true});placedBoxes.push({left:Math.max(0,r.x1-clearance),top:Math.max(0,r.y1-clearance),right:Math.min(graphWidth,r.x2+clearance),bottom:Math.min(graphHeight,r.y2+clearance),reserved:true,node:true});});
      function overlaps(candidate) { return placedBoxes.some(function (box) { return !(candidate.right + 3 <= box.left || candidate.left >= box.right + 3 || candidate.bottom + 3 <= box.top || candidate.top >= box.bottom + 3); }); }
      // Reserve overflow notation before placing any session. If the compact
      // canvas has no safe slot, overflow remains textual-only rather than
      // falling back onto a control or another ghost.
      if (!overflowGhost) { overflowGhost = document.createElement("span"); overflowGhost.className = "okf-presence-ghost okf-presence-ghost--overflow"; var reservedLabel = document.createElement("span"); reservedLabel.className = "okf-presence-ghost__label"; overflowGhost.appendChild(reservedLabel); ghostLayer.appendChild(overflowGhost); }
      // Reserve the longest final label, not an abbreviated placeholder whose
      // later expansion could cross the plot boundary or another collaborator.
      overflowGhost.firstChild.textContent = "128 collaborators nearby"; overflowGhost.hidden = false;
      var markerWidth = Math.max(54, overflowGhost.offsetWidth), markerHeight = Math.max(20, overflowGhost.offsetHeight), markerBox = null;
      for (var my = clearance; my <= graphHeight - markerHeight - clearance && !markerBox; my += markerHeight + clearance) for (var mx = clearance; mx <= graphWidth - markerWidth - clearance; mx += markerWidth + clearance) { var markerProbe={left:mx,top:my,right:mx+markerWidth,bottom:my+markerHeight}; if(!overlaps(markerProbe)){markerBox=markerProbe;break;} }
      if (markerBox) { placedBoxes.push(markerBox); overflowGhost.style.transform="translate3d("+markerBox.left+"px,"+markerBox.top+"px,0)"; } else { overflowGhost.hidden=true; }
      remote.forEach(function (row) {
        var label = row.kind === "agent" ? "Agent" : (row.label || "Browser collaborator");
        var item = document.createElement("li");
        var name = document.createElement("strong"); name.textContent = label; item.appendChild(name);
        var position = row.kind === "browser" ? ghostPosition(row) : null;
        var region = position ? ((position.y < .34 ? "upper" : position.y > .66 ? "lower" : "center") + " " + (position.x < .34 ? "left" : position.x > .66 ? "right" : "center")).replace("center center","center") : "";
        if (row === summaryRow) { collaboratorCompact.textContent = label + " · " + (row.state || "idle") + (region ? " · " + region : "") + (row.focus ? " · " + row.focus : ""); setMobileSummary("Agent " + (agentRow ? agentRow.state : "idle") + (agentRow&&agentRow.focus ? " " + agentRow.focus : ""), browserCount + " collaborator" + (browserCount === 1 ? "" : "s") + (row.kind === "agent" ? "" : " · " + label + (region ? " · " + region : ""))); }
        item.appendChild(document.createTextNode(" · " + (row.state || "idle") + (region ? " · " + region : "") + (row.focus ? " · " + row.focus : "")));
        collaboratorList.appendChild(item);
        if (row.kind !== "browser") return;
        if (!position) return;
        seen[row.session_id] = true;
        var ghost = ghostNodes[row.session_id];
        if (!ghost) {
          ghost = document.createElement("span"); ghost.className = "okf-presence-ghost";
          var marker = document.createElement("span"); marker.className = "okf-presence-ghost__marker";
          var text = document.createElement("span"); text.className = "okf-presence-ghost__label"; text.textContent = label;
          ghost.appendChild(marker); ghost.appendChild(text); ghostLayer.appendChild(ghost); ghostNodes[row.session_id] = ghost;
        } else { ghost.querySelector(".okf-presence-ghost__label").textContent = label; }
        var constrained = graphWidth < 600 || parseFloat(getComputedStyle(document.documentElement).fontSize) >= 24;
        ghost.classList.toggle("okf-presence-ghost--compact", constrained);
        if (constrained) { var discriminator=label.replace(/^Viewer\s+/i,"").replace(/[^A-Za-z0-9]/g,"").slice(0,4); ghost.querySelector(".okf-presence-ghost__label").textContent="◇ "+(discriminator||"Here"); }
        var boxWidth = graphWidth, boxHeight = graphHeight;
        // A previously unplaced ghost has no measurable box while hidden.
        // Measure its real current label before choosing a collision-free slot.
        ghost.hidden = false;
        var width = Math.max(24, ghost.offsetWidth), height = Math.max(20, ghost.offsetHeight);
        var anchorX = position.x * boxWidth, anchorY = position.y * boxHeight;
        var offsets = [[0,0],[0,height+4],[0,-height-4],[width+5,0],[-width-5,0],[width+5,height+4],[-width-5,height+4],[width+5,-height-4],[-width-5,-height-4],[0,2*(height+4)],[0,-2*(height+4)]];
        var chosen = null;
        offsets.some(function (offset) {
          var x = Math.max(clearance, Math.min(boxWidth - width - clearance, anchorX + offset[0]));
          var y = Math.max(clearance, Math.min(boxHeight - height - clearance, anchorY + offset[1]));
          var candidate = {left:x,top:y,right:x+width,bottom:y+height};
          if (!overlaps(candidate)) { chosen = candidate; return true; }
          return false;
        });
        if (!chosen) { ghost.hidden = true; overflowCount++; return; }
        ghost.hidden = false; placedBoxes.push(chosen);
        ghost.classList.toggle("okf-presence-ghost--flip-x", position.x > .72);
        ghost.classList.toggle("okf-presence-ghost--flip-y", position.y > .72);
        ghost.style.transform = "translate3d(" + Math.round(chosen.left) + "px," + Math.round(chosen.top) + "px,0)";
      });
      Object.keys(ghostNodes).forEach(function (id) { if (!seen[id]) { ghostNodes[id].remove(); delete ghostNodes[id]; } });
      if (overflowCount) {
        overflowGhost.firstChild.textContent = overflowCount + " collaborator" + (overflowCount===1?"":"s") + " nearby";
        overflowGhost.hidden = !markerBox;
      } else if (overflowGhost) { overflowGhost.remove(); overflowGhost = null; }
      collaboratorRegion.hidden = snapshot && snapshot.online === false;
      mobilePresenceBar.hidden = snapshot && snapshot.online === false;
    }
    function schedulePresenceProjection(){if(!lastPresenceSnapshot||presenceRenderFrame)return;presenceRenderFrame=requestAnimationFrame(function(){presenceRenderFrame=null;renderPresenceSnapshot(lastPresenceSnapshot);});}
    if (window.ResizeObserver) new ResizeObserver(schedulePresenceProjection).observe(graphEl);
    if (window.visualViewport) window.visualViewport.addEventListener("resize",schedulePresenceProjection);
    window.addEventListener("resize",schedulePresenceProjection);
    if (document.fonts) { document.fonts.ready.then(schedulePresenceProjection); document.fonts.addEventListener&&document.fonts.addEventListener("loadingdone",schedulePresenceProjection); }
    cy.on("render zoom pan",schedulePresenceProjection);
    if (livePresenceHub) {
      window.okfLoomLive.on("presence", function (p) {
        renderPresenceSnapshot(p);
        var records = p && p.online !== false && Array.isArray(p.records) ? p.records : [];
        var agent = records.find(function (row) { return row && row.kind === "agent"; });
        var focus = agent && agent.focus ? agent.focus : null;
        if (focus === presenceFocus) return;
        applyPresenceFocus(focus);
      });
    }

      // ---- Phase 5: the canvas consumes live graph events ---
      // Phase 4 compatibility boundary: existing Phase 5 graph/comment SSE
      // continues to own acquisition and notification. The Phase 4 controller
      // only reconciles accepted refresh deletions/metadata into interaction
      // state; it does not add events, polling, presence, animation, or tour work.
      // A `graph` SSE event means the bundle's structure changed (create /
      // remove / link edits via CLI mutators or the studio). Refetch
      // graph.json, DIFF it against the canvas, animate additions in and
      // removals out — and NEVER yank the viewport: a chip offers the
      // re-layout instead. Signal-engine normalisations recompute on the
      // next full load; the visual encoding refresh below keeps weights,
      // orphan cues, and typed-edge hues consistent for the delta.
      // Latest-wins fence for overlapping/late DATA_URL refreshes: a `graph`
      // burst (or a slow response) can leave two fetches in flight. Only the
      // response whose token is still current may mutate graph data / detail /
      // status — a stale earlier response is dropped (same ownership model as
      // runLayoutNow's layoutSeq). `refreshStats` mirrors `layoutStats` for
      // tests/diagnostics only.
      var _graphRefreshSeq = 0;
      var refreshStats = { started: 0, applied: 0, skippedStale: 0, timedOut: 0 };
      var GRAPH_REFRESH_DEADLINE = Number((window.OKF_LOOM_TEST_DEADLINES || {}).graphRefresh || 10000);
      var graphDeltaGeneration = 0, graphDeltaPending = 0;
      var graphDeltaAnimations = Object.create(null);
      function settleGraphDelta(id, owner, element, remove) {
        var current = graphDeltaAnimations[id];
        if (!current || current.owner !== owner) return;
        clearTimeout(current.timer);
        delete graphDeltaAnimations[id];
        graphDeltaPending = Math.max(0, graphDeltaPending - 1);
        try { element.stop(); element.removeClass("okf-delta-exit"); element.removeStyle("opacity width height"); } catch (e) {}
        if (remove && owner === graphDeltaGeneration && !bundle.nodes.some(function (n) { return n.data.id === id; })) {
          try { cy.remove(element); } catch (e) {}
        }
        if (!graphDeltaPending && !graphRefreshController && graphPageActive) publishGraphCaptureReady("ready", true);
      }
      function cancelGraphDelta(id) {
        var current = graphDeltaAnimations[id];
        if (current) settleGraphDelta(id, current.owner, current.element, false);
      }
      function animateGraphDelta(element, id, owner, remove) {
        cancelGraphDelta(id);
        if (REDUCED_MOTION) { if (remove) cy.remove(element); return; }
        var width = Number(element.style("width")) || 30, height = Number(element.style("height")) || 30;
        graphDeltaPending++;
        var record = { owner: owner, element: element, timer: null, remove: remove };
        graphDeltaAnimations[id] = record;
        if (remove) element.addClass("okf-delta-exit");
        else element.style({ opacity: 0, width: width * 0.72, height: height * 0.72 });
        var target = remove ? { opacity: 0, width: width * 0.72, height: height * 0.72 } : { opacity: 1, width: width, height: height };
        try { element.animate({ style: target }, { duration: 300, easing: "ease-in-out", complete: function () { settleGraphDelta(id, owner, element, remove); } }); }
        catch (e) { settleGraphDelta(id, owner, element, remove); return; }
        record.timer = setTimeout(function () { settleGraphDelta(id, owner, element, remove); }, 360);
      }
      window.__okfCancelGraphDeltas = function () { Object.keys(graphDeltaAnimations).forEach(function(id){var current=graphDeltaAnimations[id];if(current)settleGraphDelta(id,current.owner,current.element,current.remove);});graphDeltaGeneration++; };
      var _graphChipEl = null;
      function showGraphChip(text) {
        if (!_graphChipEl) {
          _graphChipEl = document.createElement("div");
          _graphChipEl.className = "okf-graph-updated";
          var span = document.createElement("span");
          _graphChipEl.appendChild(span);
          var btn = document.createElement("button");
          btn.type = "button";
          btn.textContent = "Re-run layout";
          btn.addEventListener("click", function () {
            hideGraphChip();
            runLayoutNow(false, false, true);
          });
          _graphChipEl.appendChild(btn);
          var x = document.createElement("button");
          x.type = "button";
          x.textContent = "×";
          x.setAttribute("aria-label", "Dismiss");
          x.addEventListener("click", hideGraphChip);
          _graphChipEl.appendChild(x);
          container.appendChild(_graphChipEl);
        }
        _graphChipEl.firstChild.textContent = text;
        _graphChipEl.hidden = false;
      }
      function hideGraphChip() { if (_graphChipEl) _graphChipEl.hidden = true; }

          function refreshGraphFromServer(options) {
          options = options || {};
          if (!DATA_URL || !graphPageActive) return Promise.resolve(false);
          var refreshFocus=document.activeElement,refreshFocusSolo=!!(refreshFocus&&refreshFocus.classList&&refreshFocus.classList.contains("okf-filter-solo")),refreshFocusOwner=refreshFocus&&refreshFocus.closest&&refreshFocus.closest('[aria-label="Relationship filters"]'),refreshFocusValue=refreshFocusOwner?(refreshFocusSolo?refreshFocus.dataset.filterSoloValue:refreshFocus.dataset.filterValue):document.body.dataset.okfRelationshipFilterFocus;if(!refreshFocusOwner)refreshFocusSolo=document.body.dataset.okfRelationshipFilterFocusSolo==="1";
          if (graphRefreshController) graphRefreshController.abort();
          var controller = new AbortController();
          graphRefreshController = controller;
          var requestOwner = ++graphRequestGeneration;
          var seq = ++_graphRefreshSeq;
          var deadlineClassified = false;
          function classifyDeadline() {
            if (deadlineClassified || !controller.signal.aborted || controller.signal.reason !== "deadline" || !graphPageActive || requestOwner !== graphRequestGeneration || seq !== _graphRefreshSeq) return false;
            deadlineClassified = true;refreshStats.timedOut++;
            document.body.dataset.okfGraphCaptureReady = graphDeltaPending ? "loading" : lastAcceptedCaptureReadiness.graph;
            document.body.dataset.okfCaptureReady = graphDeltaPending ? "loading" : lastAcceptedCaptureReadiness.page;
            document.body.dataset.okfGraphRefreshError = "graph-refresh-timeout";
            return true;
          }
          var deadlineTimer = setTimeout(function () { controller.abort("deadline"); classifyDeadline(); }, GRAPH_REFRESH_DEADLINE);
          refreshStats.started++;
          publishGraphCaptureReady("loading");
          return fetch(GRAPH_REQUEST_URL, { signal: controller.signal, cache: "no-cache", headers: { Accept: "application/json" } })
            .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
              .then(function (fresh) {
            // A newer refresh started while this one was in flight → this
            // response is stale; never let it mutate graph/detail/status.
                if (!graphPageActive || controller.signal.aborted || requestOwner !== graphRequestGeneration || seq !== _graphRefreshSeq) { refreshStats.skippedStale++; return; }
                  fresh = graphCandidate(fresh);
                  if (DATA_URL === "/__data/graph.json") {
                    window.okfGraphData = Promise.resolve(fresh);
                    window.dispatchEvent(new CustomEvent("okf-loom:graph-data", { detail: fresh }));
                  }
                  delete document.body.dataset.okfGraphRefreshError;
                  var previousNodeIds = bundle.nodes.map(function (n) { return n.data.id; }).sort().join("\n");
                  var freshNodeIds = fresh.nodes.map(function (n) { return n.data.id; }).sort().join("\n");
                  var previousEdgeIds = bundle.edges.map(function (e) { return e.data.source + "\u0000" + e.data.target; }).sort().join("\n");
                  var freshEdgeIdsSignature = fresh.edges.map(function (e) { return e.data.source + "\u0000" + e.data.target; }).sort().join("\n");
                  var structuralChanged = previousNodeIds !== freshNodeIds || previousEdgeIds !== freshEdgeIdsSignature;
                  refreshStats.applied++;
                  authoritativePayloadNodeCount = fresh.nodes.length;
              var freshRevision = String(fresh.bundle_revision || fresh.revision || "");
              if (freshRevision && graphBundleRevision && freshRevision !== graphBundleRevision) {
                teardownGraphLifecycle("graph-revision-change", { invalidateProvider: true });
              }
              if (freshRevision) graphBundleRevision = freshRevision;
                var freshIds = {}, deltaOwner = ++graphDeltaGeneration;
                var added = 0, removed = 0, changed = 0;
              // Update the shared lookups the detail panel + tooltips read.
                bundle = fresh;
                document.body.dataset.okfRevision = freshRevision;
                document.body.dataset.okfCaptureRevision = freshRevision;
                if (cancelGraphBodies) cancelGraphBodies();
                bodies = fresh.bodies || {};
                reservedDocuments = graphDocuments(fresh);
              backlinks = fresh.backlinks;
              outgoing = fresh.outgoing;
              recomputeLodState(fresh);
              deriveDensityState(fresh);
              nodeIndex = {};
              // Reconcile an element's complete accepted/material datum while
              // retaining Cytoscape identity, position, classes and selection.
              // Runtime-owned visual fields are supplied by elemFor or rebuilt
              // by applyVisualEncoding immediately below.
              function reconcileData(element, next) {
                var current = element.data(), didChange = false;
                Object.keys(current).forEach(function (key) {
                  if (key === "id" || key === "source" || key === "target") return;
                  if (!Object.prototype.hasOwnProperty.call(next, key)) {
                    element.removeData(key); didChange = true;
                  }
                });
                Object.keys(next).forEach(function (key) {
                  if (JSON.stringify(current[key]) !== JSON.stringify(next[key])) {
                    element.data(key, next[key]); didChange = true;
                  }
                });
                return didChange;
              }
              (fresh.nodes || []).forEach(function (n) {
              var id = n.data.id;
              freshIds[id] = true;
              nodeIndex[id] = n.data;
                var existing = cy.getElementById(id);
                if (existing && existing.length) {
                  cancelGraphDelta(id);
                  if (!shownIds[id]) { cy.remove(existing); removed++; return; }
                  // Live-edited metadata: refresh label/colour/description.
                var el2 = elemFor(n);
                if (reconcileData(existing, el2.data)) changed++;
              } else if (!lodActive || shownIds[id]) {
                shownIds[id] = true;
                var ne = cy.add(elemFor(n));
                // Place near a linked neighbour so the node appears to grow
                // out of its context rather than teleporting in.
                var nb = (outgoing[id] || []).concat(backlinks[id] || []);
                for (var i = 0; i < nb.length; i++) {
                  var anchor = cy.getElementById(nb[i]);
                  if (anchor && anchor.length) {
                    var ap = anchor.position();
                    ne.position({ x: ap.x + 40 + Math.floor(60 * ((id.length % 7) / 7)), y: ap.y + 40 });
                    break;
                  }
                }
                animateGraphDelta(ne, id, deltaOwner, false);
                added++;
              }
            });
              cy.nodes().forEach(function (n) {
              if (freshIds[n.id()]) return;
              removed++;
              delete shownIds[n.id()];
                animateGraphDelta(n, n.id(), deltaOwner, true);
              });
              buildNodeIndex(fresh);
              if (selectedConceptId && freshIds[selectedConceptId]) {
                renderSignalExplain(selectedConceptId);
                if (LIVE_GRAPH) showSelectedBody(selectedConceptId, document.getElementById("detail-body"), document.getElementById("detail-okf-metadata"), options.retryBodyRevision);
              }
              if (selectedConceptId && !freshIds[selectedConceptId]) {
                // Phase 4 compatibility boundary for the existing Phase 5
                // refresh: reconcile interaction state after acceptance only.
                teardownGraphLifecycle("refresh-selection-deleted", { invalidateProvider: false });
                var deletedWasFocusRoot=controlState.focusEnabled&&focusRoot===selectedConceptId;
                dispatchInteraction({type:"CLEAR",quietUrl:true});
                if(deletedWasFocusRoot)reconcileDeletedFocus();
                var survivor = document.getElementById("okf-search") || document.querySelector("[data-graph-pane]");
                if (survivor) survivor.focus();
              }
              if(interactionState.active&&!freshIds[interactionState.active]){interactionState.active=null;syncInteractionIndex();}
              if(focusRoot&&!freshIds[focusRoot]){setStatusNote("The neighborhood concept was removed. Showing all concepts.");reconcileDeletedFocus();}
              if((interactionState.pathStart&&!freshIds[interactionState.pathStart])||(interactionState.pathEnd&&!freshIds[interactionState.pathEnd])){setStatusNote("The path was cleared because an endpoint was removed.");clearPath();writeInteractionUrl("replace");}
              writeInteractionUrl("replace");
            // Edge delta between currently-shown nodes.
            var haveEdge = {};
            cy.edges().forEach(function (e) { haveEdge[e.id()] = e; });
            var freshEdgeIds = {};
            (fresh.edges || []).forEach(function (e) {
              if (!shownIds[e.data.source] || !shownIds[e.data.target]) return;
                var key = runtimeEdgeId(e.data);
              if (freshEdgeIds[key]) return;
              freshEdgeIds[key] = true;
              if (!haveEdge[key]) {
                var ed = Object.assign({}, e.data, { id: key, source: e.data.source, target: e.data.target, weight: 0.5, bundleLabel: e.data.multiplicity > 1 ? String(e.data.multiplicity) : "" });
                try { cy.add({ data: ed }); added++; } catch (err) {}
              } else {
                var edgeData = Object.assign({}, e.data, {
                  id: key, source: e.data.source, target: e.data.target,
                  weight: haveEdge[key].data("weight"),
                  bundleLabel: e.data.multiplicity > 1 ? String(e.data.multiplicity) : ""
                });
                if (reconcileData(haveEdge[key], edgeData)) changed++;
              }
            });
            Object.keys(haveEdge).forEach(function (key) {
              if (!freshEdgeIds[key]) { try { cy.remove(haveEdge[key]); removed++; } catch (err) {} }
            });
            rebuildBundleIndex();
            if(activeBundleEdgeId&&!bundlePanel.hidden){var refreshedBundle=cy.getElementById(activeBundleEdgeId);if(refreshedBundle&&refreshedBundle.length)renderBundleDetail(refreshedBundle,{focusPanel:false});else collapseBundleDetail(true);}
                if (added || removed || changed || structuralChanged) {
              recomputeAcceptedMetrics(fresh);
              refreshRelationFilterControls();updateLegend(true);
              invalidateLensCache();
              computeCommunities();
              applyVisualEncoding();
              applyFilters();
              if(focusRoot){var refreshedRoot=cy.getElementById(focusRoot);if(!refreshedRoot.length||refreshedRoot.hasClass("dim")){setStatusNote("The neighborhood concept is hidden by the current filters. Showing all concepts.");reconcileDeletedFocus();}}
              if(interactionState.pathStart&&interactionState.pathEnd&&validInteractionId(interactionState.pathStart)&&validInteractionId(interactionState.pathEnd)){var keptStart=interactionState.pathStart,keptEnd=interactionState.pathEnd;showPathBetween(cy.getElementById(keptStart),cy.getElementById(keptEnd));interactionState.pathStart=keptStart;interactionState.pathEnd=keptEnd;}
              applyDensityProfile(true);
                // Structural refreshes deliberately preserve the camera and do
                // not run a global layout.  The accepted model still owns
                // degree-zero placement, though: dock its current orphans now,
                // before readiness, while transition exits are excluded from
                // both the orphan set and the connected-node bounds.
                dockOrphanNodes();
                updateStatus();
                renderLensSummary();
                refreshLodPill();
                var bits = [];
              if (added) bits.push(added + " added");
              if (removed) bits.push(removed + " removed");
                if (changed) bits.push("metadata updated");
                if (!bits.length && structuralChanged) bits.push("inventory updated");
                showGraphChip("Graph updated (" + bits.join(", ") + ")");
                if(refreshFocusValue){var acceptedBase=document.querySelector('[aria-label="Relationship filters"] [data-filter-value="'+CSS.escape(refreshFocusValue)+'"]'),acceptedTarget=refreshFocusSolo&&acceptedBase?acceptedBase.parentElement.querySelector('.okf-filter-solo'):acceptedBase;if(acceptedTarget)acceptedTarget.focus();}
                if (structuralChanged) {
                  if (authoritativePayloadNodeCount === 0) settleEmptyGraph();
                  else layoutSettled = true;
                }
              }
              // Derived canvas/index/detail surfaces are now synchronized.
              // A superseded request is forbidden from publishing readiness.
              window.requestAnimationFrame(function () {
                if (graphPageActive && !controller.signal.aborted && requestOwner === graphRequestGeneration && seq === _graphRefreshSeq) {
                  if(refreshFocusValue){var finalBase=document.querySelector('[aria-label="Relationship filters"] [data-filter-value="'+CSS.escape(refreshFocusValue)+'"]'),finalTarget=refreshFocusSolo&&finalBase?finalBase.parentElement.querySelector('.okf-filter-solo'):finalBase;if(finalTarget)finalTarget.focus();}
                  if(refreshFocusValue)setTimeout(function(){if(!graphPageActive||requestOwner!==graphRequestGeneration||seq!==_graphRefreshSeq)return;var settledBase=document.querySelector('[aria-label="Relationship filters"] [data-filter-value="'+CSS.escape(refreshFocusValue)+'"]'),settledTarget=refreshFocusSolo&&settledBase?settledBase.parentElement.querySelector('.okf-filter-solo'):settledBase;if(settledTarget)settledTarget.focus();},0);
                  var acceptedState=layoutSettled && !graphDeltaPending ? "ready" : "loading";
                  publishGraphCaptureReady(acceptedState, acceptedState === "ready");
                }
              });
              return true;
            })
              .catch(function (error) {
                // Only the still-current owner may restore the coherent
                // last-good readiness snapshot. Superseded, aborted and
                // pagehidden requests publish nothing at all.
                if (controller.signal.aborted) {
                  classifyDeadline();
                  return;
                }
                if (!graphPageActive || controller.signal.aborted || requestOwner !== graphRequestGeneration || seq !== _graphRefreshSeq) {
                  refreshStats.skippedStale++;
                  return;
                }
                document.body.dataset.okfGraphCaptureReady = lastAcceptedCaptureReadiness.graph;
                document.body.dataset.okfCaptureReady = lastAcceptedCaptureReadiness.page;
                document.body.dataset.okfGraphRefreshError = error && error.okfPhase || "graph-refresh";
              })
              .finally(function () { clearTimeout(deadlineTimer); if (graphRefreshController === controller) { graphRefreshController = null;if(graphPageActive&&!graphDeltaPending)publishGraphCaptureReady("ready",true); }if(graphPageActive&&requestOwner===graphRequestGeneration&&refreshFocusValue){var doneBase=document.querySelector('[aria-label="Relationship filters"] [data-filter-value="'+CSS.escape(refreshFocusValue)+'"]'),doneTarget=refreshFocusSolo&&doneBase?doneBase.parentElement.querySelector('.okf-filter-solo'):doneBase;if(doneTarget)doneTarget.focus();} });
        }
        window.__okfRefreshGraph = refreshGraphFromServer;
        function scheduleGraphRefresh() {
          if (!graphPageActive) return;
          if (graphRefreshDebounceTimer) clearTimeout(graphRefreshDebounceTimer);
          graphRefreshDebounceTimer = setTimeout(function () { graphRefreshDebounceTimer = null; if (graphPageActive) refreshGraphFromServer(); }, 700);
        }
    if (window.okfLoomLive && typeof window.okfLoomLive.on === "function") {
        window.okfLoomLive.on("graph", scheduleGraphRefresh);
        // Comment lifecycle events change only graph-derived count metadata,
        // but share the exact same debounced request owner and stale fence.
        // This is deliberately not broad Phase 5 graph liveness.
        window.okfLoomLive.on("comment", scheduleGraphRefresh);
    }

    // No auto-selected node at boot — the first thing the right
    // pane shows is the ACTIVE LENS SUMMARY (the ranked answers), which
    // the old highest-degree auto-select used to bury. Clicking any node
    // or summary row opens its page as before.

    // ---- Phase 5: first-visit micro-tour --------------------------------
    // Three coach marks (lenses / hover+path / open), shown once per
    // browser and dismissed forever via localStorage. Zero-dependency: a
    // single repositioned card, Esc or ✕ skips.
    (function microTour() {
      var KEY = "okfGraphTourDone:v2:" + encodeURIComponent(String(bundle.name || "bundle")) + ":schema-3";
      // Portable artifacts must open directly onto usable graph controls.
      // The live studio keeps the first-visit modal and its focus contract.
      if (MODE === "static" || IS_SINGLE) return;
      if (GRAPH_EMPTY) return;
      var steps = [
        { title: "Choose a lens", text: "Choose a lens to reframe this graph for themes, flow, bridges, recent work, or an explored neighborhood.", mobileText: "Open Details to choose a lens, then reframe this graph for themes, flow, bridges, recent work, or an explored neighborhood.", anchor: ".okf-signal__lenses", mobileAnchor: '[data-graph-pane="details"]' },
        { title: "Explore connections", text: "Select, tap, or hover a concept to reveal its neighbors. With a keyboard, use the arrow keys and Shift+Space to trace a path.", anchor: "#okf-graph", mobileAnchor: "#okf-graph" },
        { title: "Open a concept", text: "Select or tap a concept, then open it from the detail action. With a keyboard, press Enter.", mobileText: "Open Concepts, choose a concept, then open it. With a keyboard, press Enter.", anchor: "#okf-detail", mobileAnchor: '[data-graph-pane="concepts"]' },
      ];
      var idx = 0, active = false, replaying = false;
      var overlayOwner = {
        id: "graph-tour",
        close: function () { finish(true); },
        focusIn: function () { if (active) try { next.focus({preventScroll:true}); } catch (e) {} }
      };
      var storageAvailable = true;
      try { if (window.localStorage.getItem(KEY)) replaying = true; } catch (e) { storageAvailable = false; }
      // Remember what had focus so the tour can restore it on close (a11y:
      // dialogs must move focus in on open and hand it back on dismiss).
      var previousFocus = null, previousPane = null;
      var scrim = null, modal = false, replayStart = false;
      var inerted = [];
      var card = document.createElement("div");
      card.className = "okf-graph-tour";
      card.setAttribute("data-at", "left");
      card.setAttribute("role", "dialog");
      // Focus is trapped inside the tour, so it behaves as a modal dialog.
      card.setAttribute("aria-modal", "false");
      card.setAttribute("aria-label", "Graph tour");
      card.setAttribute("tabindex", "-1");
      var h = document.createElement("strong");
      h.id = "okf-graph-tour__title";
      var p = document.createElement("p");
      p.id = "okf-graph-tour__desc";
      // Name stays "Graph tour"; the step title + body are the description.
      card.setAttribute("aria-describedby", "okf-graph-tour__desc");
      var announce = document.createElement("p");
      announce.className = "okf-sr-only okf-graph-tour__announce";
      announce.setAttribute("aria-hidden", "true");
      var handle = document.createElement("span"); handle.className = "okf-graph-tour__handle"; handle.setAttribute("aria-hidden", "true");
      var nav = document.createElement("div");
      nav.className = "okf-graph-tour__nav";
      var dots = document.createElement("span");
      dots.className = "okf-graph-tour__dots";
      var next = document.createElement("button");
      next.type = "button";
      next.className = "okf-graph-tour__next";
      var skip = document.createElement("button");
      skip.type = "button";
      skip.className = "okf-graph-tour__skip";
      skip.textContent = "Skip";
      var close = document.createElement("button");
      close.type = "button"; close.className = "okf-graph-tour__close";
      close.textContent = "×"; close.setAttribute("aria-label", "Close graph tour");
      var back = document.createElement("button");
      back.type = "button"; back.className = "okf-graph-tour__back"; back.textContent = "Back";
      var peek = document.createElement("button");
      peek.type = "button"; peek.className = "okf-graph-tour__peek"; peek.textContent = "View graph";
      peek.setAttribute("aria-expanded", "true"); peek.setAttribute("aria-label", "Temporarily collapse tour to view graph");
      nav.appendChild(dots); nav.appendChild(back); nav.appendChild(skip); nav.appendChild(next);
      card.appendChild(handle); card.appendChild(close); card.appendChild(h); card.appendChild(p); card.appendChild(announce); card.appendChild(peek); card.appendChild(nav);
      var focusables = [close, peek, back, skip, next];
      function restoreFocus() {
        // Hand focus back to the opener when it is still a real, connected
        // and visible control. On narrow layouts search lives in the collapsed
        // secondary panel, so the active primary pane tab is the stable
        // fallback rather than an off-screen control.
        var visibleSearch = document.getElementById("okf-search");
        if (visibleSearch && visibleSearch.offsetParent === null) visibleSearch = null;
        var target = (previousFocus && previousFocus !== document.body &&
                      document.contains(previousFocus) &&
                      previousFocus.offsetParent !== null &&
                      typeof previousFocus.focus === "function")
          ? previousFocus
          : (visibleSearch ||
             (isMobileGraphViewport() && document.querySelector('[data-graph-pane="graph"]')) ||
             document.getElementById("okf-reset") || container);
        if (target && typeof target.focus === "function") {
          try { target.focus({ preventScroll: true }); }
          catch (e) { try { target.focus(); } catch (e2) {} }
        }
      }
      function cleanupSurface() {
        if (window.OKFOverlayStack) window.OKFOverlayStack.remove(overlayOwner);
        window.removeEventListener("keydown", onKey, true);
        window.removeEventListener("pagehide", onPageHide);
        window.removeEventListener("resize", onActiveResize);
        if (scrim && scrim.parentNode) scrim.parentNode.removeChild(scrim);
        else if (card.parentNode) card.parentNode.removeChild(card);
        scrim = null;
        card.classList.remove("okf-graph-tour--detail", "okf-graph-tour--sheet", "is-collapsed");
        card.setAttribute("aria-modal", "false"); peek.setAttribute("aria-expanded", "true"); peek.textContent = "View graph";
        inerted.forEach(function (entry) {
          if (!entry.node.isConnected) return;
          if (entry.inert) entry.node.setAttribute("inert", ""); else entry.node.removeAttribute("inert");
        });
        inerted = [];
        actionBar.removeAttribute("inert");
        Array.prototype.forEach.call(document.querySelectorAll("[data-tour-anchor]"), function (node) { node.removeAttribute("data-tour-anchor"); });
        try { cy.nodes().removeClass("okf-tour-target"); } catch (e) {}
        container.removeAttribute("data-tour-step");
        document.body.classList.remove("okf-graph-tour-open");
      }
      function finish(markComplete, restore) {
        if (!active) return;
        active = false;
        if (markComplete && storageAvailable) try { window.localStorage.setItem(KEY, "1"); } catch (e) { storageAvailable = false; }
        cleanupSurface();
        if (previousPane && isCompactGraphViewport()) setMobilePane(previousPane, false);
        previousPane = null;
        if (restore !== false) restoreFocus();
      }
      function availableStep(from, direction) {
        var candidate = from;
        while (candidate >= 0 && candidate < steps.length) {
          var anchor = document.querySelector(window.innerWidth <= 640 ? steps[candidate].mobileAnchor : steps[candidate].anchor);
          // A pane choice is not an unavailable graph. The explicitly opened
          // mobile tour can reveal its canvas step, then restore its invoker's
          // pane. An explicitly removed/hidden canvas still skips fail-closed.
          if (candidate === 1 && window.innerWidth <= 640 && anchor && anchor.style.display !== "none") return candidate;
          if (anchor && (anchor.offsetParent !== null || candidate === 0 || candidate === steps.length - 1)) return candidate;
          candidate += direction;
        }
        return -1;
      }
      function render() {
        var s = steps[idx];
        if (modal) setMobilePane(idx === 1 ? "graph" : (idx === 2 ? "concepts" : "details"), false);
        Array.prototype.forEach.call(document.querySelectorAll("[data-tour-anchor]"), function (node) { node.removeAttribute("data-tour-anchor"); });
        try { cy.nodes().removeClass("okf-tour-target"); } catch (e) {}
        h.textContent = s.title;
        var stepText = modal && s.mobileText ? s.mobileText : s.text;
        p.textContent = stepText;
        dots.textContent = "Step " + (idx + 1) + " of " + steps.length;
        next.textContent = idx === steps.length - 1 ? "Finish" : "Next";
        back.disabled = idx === 0;
        var anchorSelector = modal ? s.mobileAnchor : s.anchor;
        var anchor = document.querySelector(anchorSelector);
        card.setAttribute("data-anchor", anchorSelector);
        if (anchor) anchor.setAttribute("data-tour-anchor", "true");
        if (idx === 0) { var selectedLens=document.querySelector('.okf-signal__seg-item input:checked + .okf-lens-choice'); if(selectedLens)selectedLens.setAttribute("data-tour-anchor","true"); }
        if (idx === 1) try { var representative=cy.nodes().filter(function(node){return !node.hasClass("dim");}).first(); if(representative.length)representative.addClass("okf-tour-target"); } catch (e) {}
        container.setAttribute("data-tour-step", String(idx + 1));
        announce.textContent = "Graph tour, step " + (idx + 1) + " of " + steps.length + ": " + s.title + ". " + stepText;
      }
      // The tour OWNS Tab (focus trap) and Escape while open. The handler lives
      // on the card and stops Escape from bubbling to the graph's global
      // keydown handler, so Escape closes ONLY the tour (it does not also clear
      // the current selection / path the way a bare graph Escape would).
      function onKey(e) {
        if (window.OKFOverlayStack && window.OKFOverlayStack.top() !== overlayOwner) return;
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
           finish(true);
          return;
        }
         if (modal && e.key === "Tab") {
           var enabled = focusables.filter(function (button) { return !button.disabled && button.offsetParent !== null; });
           var first = enabled[0], last = enabled[enabled.length - 1];
          var active = document.activeElement;
          if (e.shiftKey) {
            if (active === first || active === card) { e.preventDefault(); last.focus(); }
          } else {
            if (active === last) { e.preventDefault(); first.focus(); }
          }
        }
      }
      next.addEventListener("click", function () {
        if (idx >= steps.length - 1) { finish(true); return; }
        var nextIdx = availableStep(idx + 1, 1);
        if (nextIdx < 0) { finish(true); return; }
        idx = nextIdx; render();
      });
      back.addEventListener("click", function () { var prior=availableStep(idx-1,-1); if(prior>=0){idx=prior;render();} });
      skip.addEventListener("click", function () { finish(true); });
      close.addEventListener("click", function () { finish(true); });
      peek.addEventListener("click", function () { var collapsed=card.classList.toggle("is-collapsed");peek.setAttribute("aria-expanded", collapsed?"false":"true");peek.textContent=collapsed?"Resume tour":"View graph"; });
      function onPageHide() { finish(false, false); }
      function onActiveResize() {
        var shouldModal = window.innerWidth <= 640;
        if (shouldModal !== modal) {
          mountSurface();
          render();
          if (modal || replayStart) {
            try { next.focus({ preventScroll: true }); } catch (e) { try { next.focus(); } catch (e2) {} }
          } else {
            restoreFocus();
          }
        }
      }
      function mountSurface() {
        if (scrim && scrim.parentNode) scrim.parentNode.removeChild(scrim);
        else if (card.parentNode) card.parentNode.removeChild(card);
        scrim = null;
        inerted.forEach(function(entry){if(entry.node.isConnected){if(entry.inert)entry.node.setAttribute("inert","");else entry.node.removeAttribute("inert");}}); inerted=[];
        card.classList.remove("okf-graph-tour--detail", "okf-graph-tour--sheet", "is-collapsed");
        modal = window.innerWidth <= 640;
        card.setAttribute("aria-modal", modal ? "true" : "false");
        peek.hidden = !modal;
        var detailPane = document.getElementById("okf-detail");
        if (!modal) {
          card.classList.add("okf-graph-tour--detail");
          /* At 641–900 the approved graph hierarchy hides the detail pane but
           * the tour remains nonmodal. Mount the coach mark on the visible,
           * bounded canvas instead of putting it inside a hidden pane. */
          if (detailPane && detailPane.offsetParent !== null) {
            detailPane.insertBefore(card, detailPane.firstChild);
          } else {
            container.appendChild(card);
          }
        } else {
          card.classList.add("okf-graph-tour--sheet"); scrim=document.createElement("div");scrim.className="okf-graph-tour-scrim";
          Array.prototype.forEach.call(document.body.children,function(node){inerted.push({node:node,inert:node.hasAttribute("inert")});node.setAttribute("inert","");});
          scrim.appendChild(card);document.body.appendChild(scrim);
        }
      }
      function startTour(fromReplay, opener) {
        if (active || !graphPageActive) return;
        var first = availableStep(0, 1);
        if (first < 0) return;
        replaying = !!fromReplay; replayStart = !!fromReplay; idx = first; active = true;
        previousFocus = opener || document.activeElement;
        previousPane = graphLayout && graphLayout.getAttribute("data-mobile-pane");
        document.body.classList.add("okf-graph-tour-open");
        mountSurface(); render();
        if (window.OKFOverlayStack) window.OKFOverlayStack.push(overlayOwner);
        window.addEventListener("keydown", onKey, true);
        window.addEventListener("pagehide", onPageHide);
        window.addEventListener("resize", onActiveResize);
        // Automatic desktop coach marks announce without stealing focus.
        // Explicit replay and every mobile modal move focus to the primary action.
        if (modal || replayStart) try { next.focus({ preventScroll: true }); } catch (e) { try { next.focus(); } catch (e2) {} }
       }
      var replay = document.createElement("button");
      replay.type = "button"; replay.className = "okf-btn okf-graph-tour-replay";
      replay.textContent = "Take a 45-second graph tour";
      replay.setAttribute("aria-label", "Take a 45-second graph tour");
      var helpGroup=document.createElement("div");helpGroup.className="okf-graph-help";helpGroup.setAttribute("role","group");helpGroup.setAttribute("aria-label","Graph help");var helpLabel=document.createElement("span");helpLabel.className="okf-graph-help__label";helpLabel.textContent="Help";helpGroup.appendChild(helpLabel);helpGroup.appendChild(replay);
      var controls = document.querySelector(".okf-graph-tools");
      var tourHome = document.createComment("okf graph tour invitation home");
      if (controls) { controls.appendChild(tourHome); controls.appendChild(helpGroup); }
      function placeTourInvitation() {
        if (ui.settingsBody) {
          if (helpGroup.parentNode !== ui.settingsBody) ui.settingsBody.appendChild(helpGroup);
        } else if (tourHome.parentNode && helpGroup.previousSibling !== tourHome) {
          tourHome.parentNode.insertBefore(helpGroup, tourHome.nextSibling);
        }
      }
      placeTourInvitation();
      window.addEventListener("resize", placeTourInvitation);
      replay.addEventListener("click", function () { startTour(true, replay); });
      window.addEventListener("pageshow", function () { if (!active) cleanupSurface(); });
      // F08: the graph is usable before onboarding. The tour has exactly one
      // explicit invitation and never mounts a scrim or steals focus on load.
    })();

    // ---- Diagnostic / test hook (non-visual; NOT a public API) ---------
    // This is a TEST/DIAGNOSTIC hook, not a stable read-only runtime API. It
    // exposes the LIVE, MUTABLE Cytoscape instance (`cy`) plus read-only helpers
    // — a control-state snapshot (`getState()`), the current `focusRoot()`, and
    // the async-layout lifecycle counters (`layoutStats`, incl. `lastAppliedSeq`)
    // — so durable browser tests can assert REAL graph-state mutations
    // (positions, data, classes) and that stale layout completions are fenced
    // (`layoutStats.skippedStale`). The tests only READ through it; callers must
    // not treat `cy` as immutable. It adds no inline script (CSP-safe) and does
    // not affect rendering. Field shape may change with the implementation.
      // Every encoding owner (legend, summary, shelf) now exists, so a later
      // palette event may repaint the whole lens encoding.
      lensPaintReady = true;
      try {
        window.__okfLoomGraph = {
          cy: cy,   // live, mutable Cytoscape core — for tests/diagnostics only
          getState: function () { try { return JSON.parse(JSON.stringify(controlState)); } catch (e) { return null; } },
          interaction: { dispatch: graphInteractionController.dispatch, snapshot: graphInteractionController.snapshot, restoreUrl: graphInteractionController.restore },
          applyLens: applyLens,
          // P1 progressive disclosure: test hooks for edge elision + path tracing.
          tracePath: function (a, b) { return showPathBetween(a, b); },
          clearPath: clearPath,
          applyEdgeElision: applyEdgeElision,
          edgeFlow: { stop: stopDirectionalEdgeFlow, acquireCaptureLease: acquireDirectionalEdgeFlowLease, releaseCaptureLease: releaseDirectionalEdgeFlowLease, active: function(){return !!edgeFlowFrame;}, lease: function(){return edgeFlowLeaseToken;} },
          backboneEdgeCount: function () { return Object.keys(computeBackbone()).length; },
          densityPolicyForCount: densityPolicyForCount,
          communityOf: function () { try { return JSON.parse(JSON.stringify(communityOf)); } catch (e) { return {}; } },
        focusRoot: function () { return focusRoot; },
        layoutStats: layoutStats,
        // Live-refresh fence counters (present only when the live layer wired
        // the DATA_URL refresh). Used to prove stale responses are dropped.
        refreshStats: (typeof refreshStats !== "undefined") ? refreshStats : null,
        refreshGraph: typeof window.__okfRefreshGraph === "function" ? window.__okfRefreshGraph : null
      };
      window.okfGraphInteraction = graphInteractionController;
      // The default Map projection above already represents an empty URL.
      // Restore only admitted URL state, not a second Map restyle/clear pass.
      // Popstate always uses the full restore owner, including an empty URL.
      // Skipping the restyle must not skip its final status commit: the
      // visible counts/text are published now, not by the 200ms debounce.
      var initialInteraction = decodeInteractionUrl();
      if (initialInteraction.lens !== "map" || initialInteraction.selected || initialInteraction.focus || initialInteraction.path || Object.keys(initialInteraction.hidden).length || Object.keys(initialInteraction.hiddenRelations).length) graphInteractionController.restore();
      else reconcileVisibleRelationshipProjection();
      // Stable, read-only capture adapter. The mutable Cytoscape core remains
      // diagnostic-only and is intentionally absent from this public shape.
      window.okfGraphCaptureState = function () {
        var interactionPending=!!hoverTimer||!!hoverTransitionTimer||fullscreenPending||!!pendingViewportRestore;
        var state = {
          initialized: true,
          nodeCount: cy.nodes().filter(function (node) { return !node.hasClass("okf-delta-exit"); }).length,
          payloadNodeCount: authoritativePayloadNodeCount,
          layoutRequired: layoutRequired,
          layoutSettled: layoutSettled && !interactionPending,
          transitionPending: graphDeltaPending > 0 || detailBodyPending,
          layoutGeneration: layoutSeq,
          appliedLayoutGeneration: lastAppliedSeq,
          initialSeedApplied: initialSeedApplied,
          fitAppliedGeneration: fitAppliedGeneration,
          suspended: captureSuspended,
          readyPublicationCount: graphReadyPublicationCount,
          presenceDiagnostic: {
            count: Object.keys(ghostNodes).length,
            sessionIds: Object.keys(ghostNodes).sort()
          }
        };
        if (detailBodyError) { state.terminalCode = "network-failure"; state.terminalPhase = "graph-body"; }
        if (layoutTerminalCode) { state.terminalCode = layoutTerminalCode; state.terminalPhase = layoutTerminalPhase; }
        return state;
      };
      if (layoutRequired && layoutSeq === 0) {
        // Publish useful, label-safe seed geometry synchronously.  The tuned
        // layout may settle later, but initial paint/reset must never expose
        // Cytoscape's tiny auto-fit while that async owner is running.
        if(cy.nodes().length<=12){syncMobileReadableStyle();compactMobileSmallGraph();applyInitialSparseSeed();initialSeedApplied=true;}
        runLayoutNow(false);
      }
        publishGraphCaptureReady(layoutSettled ? "ready" : "loading", true);
    } catch (e) {}
    initialMobileViewport = null;
  }

  // ---- helpers ----------------------------------------------------------
  function setText(id, txt) {
    var el = document.getElementById(id);
    if (el) el.textContent = txt;
  }
  function debounce(fn, ms) {
    var t = null;
    return function () {
      var ctx = this, args = arguments;
      if (t) clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms);
    };
  }
  // Tiny createElement helper for the Signal-controls panel. Children are
  // appended as text nodes (strings) or elements — never innerHTML, so no
  // untrusted markup path is introduced (CSP-safe, no inline HTML).
  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null) return;
      if (k === "class") node.className = v;
      else node.setAttribute(k, v);
    });
    (kids || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  // ---- Governed keys (P1-3 iter-1) -------------------------------------
  // Renders §7 governed keys (aliases / entities / provenance / citations /
  // relations) into #detail-governed, mirroring the concept page's
  // _render_governed_keys treatment (contract-runtime-parity). Built with
  // createElement + textContent so untrusted alias/entity/note text cannot
  // XSS the panel (the data ships as JSON from the server; URLs were
  // already _safe_url-sanitized server-side).
  function makeLabel(text) {
    var s = document.createElement("span");
    s.className = "okf-governed-label";
    s.textContent = text;
    return s;
  }

  function isWebUrl(u) {
    return typeof u === "string" && /^https?:\/\//i.test(u);
  }

  function renderGoverned(container, data) {
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);
    var sections = [];

    // aliases - pills
    var aliases = (data && data.aliases) || [];
    if (aliases.length) {
      var div = document.createElement("div");
      div.className = "okf-governed okf-aliases";
      div.appendChild(makeLabel("Also known as:"));
      aliases.forEach(function (a) {
        var pill = document.createElement("span");
        pill.className = "okf-alias-pill";
        pill.textContent = a;
        div.appendChild(document.createTextNode(" "));
        div.appendChild(pill);
      });
      sections.push(div);
    }

    // entities - <dl> (term = label + kind chip; dd = aliases)
    var entities = (data && data.entities) || [];
    if (entities.length) {
      var wrap = document.createElement("div");
      wrap.className = "okf-governed okf-governed--block okf-entities";
      wrap.appendChild(makeLabel("Entities:"));
      var dl = document.createElement("dl");
      dl.className = "okf-entity-list";
      entities.forEach(function (ent) {
        var dt = document.createElement("dt");
        var lbl = document.createElement("span");
        lbl.className = "okf-entity";
        lbl.textContent = ent.label || ent.id || "";
        dt.appendChild(lbl);
        if (ent.kind) {
          var kind = document.createElement("span");
          kind.className = "okf-entity-kind";
          kind.textContent = ent.kind;
          dt.appendChild(kind);
        }
        var dd = document.createElement("dd");
        if (ent.aliases && ent.aliases.length) {
          var al = document.createElement("span");
          al.className = "okf-entity-aliases";
          al.textContent = "(" + ent.aliases.join(", ") + ")";
          dd.appendChild(al);
        }
        dl.appendChild(dt);
        dl.appendChild(dd);
      });
      wrap.appendChild(dl);
      sections.push(wrap);
    }

    // provenance - stacked <ul> (source link / note / time)
    var provenance = (data && data.provenance) || [];
    if (provenance.length) {
      var pwrap = document.createElement("div");
      pwrap.className = "okf-governed okf-governed--block okf-provenance";
      pwrap.appendChild(makeLabel("Sources:"));
      var ul = document.createElement("ul");
      ul.className = "okf-provenance-list";
      provenance.forEach(function (p) {
        var li = document.createElement("li");
        li.className = "okf-provenance-item";
        if (p.source) {
          if (isWebUrl(p.source)) {
            var a = document.createElement("a");
            a.href = p.source; a.rel = "noopener";
            a.textContent = p.source;
            li.appendChild(a);
          } else {
            var s = document.createElement("span");
            s.className = "okf-provenance-source";
            s.textContent = p.source;
            li.appendChild(s);
          }
        }
        if (p.note) {
          var n = document.createElement("span");
          n.className = "okf-provenance-note";
          n.textContent = p.note;
          li.appendChild(n);
        }
        if (p.timestamp) {
          var t = document.createElement("time");
          t.className = "okf-provenance-time";
          t.textContent = p.timestamp;
          li.appendChild(t);
        }
        if (li.childNodes.length) ul.appendChild(li);
      });
      if (ul.childNodes.length) { pwrap.appendChild(ul); sections.push(pwrap); }
    }

    // citations - numbered <ol>
    var citations = (data && data.citations) || [];
    if (citations.length) {
      var cwrap = document.createElement("div");
      cwrap.className = "okf-governed okf-governed--block okf-citations";
      cwrap.appendChild(makeLabel("Citations:"));
      var ol = document.createElement("ol");
      ol.className = "okf-citation-list";
      citations.forEach(function (c) {
        var li = document.createElement("li");
        li.className = "okf-citation";
        if (c.id) {
          var id = document.createElement("span");
          id.className = "okf-cite-id";
          id.textContent = "[" + c.id + "]";
          li.appendChild(id);
        }
        if (c.text) {
          var tx = document.createElement("span");
          tx.className = "okf-cite-text";
          tx.textContent = c.text;
          li.appendChild(tx);
        }
        if (isWebUrl(c.url)) {
          var ua = document.createElement("a");
          ua.href = c.url; ua.rel = "noopener";
          ua.textContent = c.url;
          li.appendChild(ua);
        }
        if (li.childNodes.length) ol.appendChild(li);
      });
      if (ol.childNodes.length) { cwrap.appendChild(ol); sections.push(cwrap); }
    }

    // relations - typed chips
    var relations = (data && data.relations) || [];
    if (relations.length) {
      var rwrap = document.createElement("div");
        rwrap.className = "okf-governed okf-governed--block okf-relations-fm";
        rwrap.appendChild(makeLabel("Relations:"));
        relations.forEach(function (r, i) {
          var rel = document.createElement("span");
        rel.className = "okf-relation";
        var chip = document.createElement("span");
        chip.className = "okf-rel-type";
        chip.textContent = r.type || "related";
        rel.appendChild(chip);
        rel.appendChild(document.createTextNode(" → "));
        var tgt = document.createElement("span");
        tgt.textContent = r.target || "";
        rel.appendChild(tgt);
        if (r.via) {
          var det = document.createElement("span");
          det.className = "okf-rel-detail";
          det.textContent = " " + r.via;
          rel.appendChild(det);
        }
        rwrap.appendChild(rel);
      });
      sections.push(rwrap);
    }

    sections.forEach(function (s) { container.appendChild(s); });
    container.hidden = sections.length === 0;
  }
})();
