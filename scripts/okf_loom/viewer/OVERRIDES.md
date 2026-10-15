# OKF Viewer — Extension & Override Points

The viewer supports four active extension mechanisms; §3 records the retired
type-colour override. All override files live under
`<bundle_root>/.okf-loom/viewer/`, so a bundle can be self-contained without
forking okf-loom.

> **Precedence:** bundle overrides win over the built-in assets, every time.

---

## 1. Template overrides

Drop a file at `.okf-loom/viewer/templates/<name>.html` to replace the built-in
template of the same name. Recognised names:

| Name                | Used by                          |
|---------------------|----------------------------------|
| `single_file.html`  | `render_single_file`             |
| `concept_page.html` | serve / spa / static concept page|
| `index_page.html`   | serve / spa / static index       |
| `search_page.html`  | serve search results             |
| `graph_page.html`   | `/__graph` full-page graph       |

Templates use **`str.replace` placeholders** (NOT `string.Template`), because
concept bodies frequently contain literal `$`. Each template documents its
placeholders in a leading HTML comment. Example — a custom concept page
footer:

```html
<!-- .okf-loom/viewer/templates/concept_page.html -->
...
<footer>© Acme Corp — internal OKF mirror</footer>
```

Two built-in elements carry behaviour that an override should keep:

- `<p class="okf-static-viewer-note">` is where exports present **Review in
  Studio** (or say why it is unavailable) from the `#okf-review-bootstrap`
  metadata the build injects before `</head>`. A template without that element
  still builds and reads normally; it simply offers no review link.
- In `single_file.html`, `#okf-single-index` and `#detail-content` are the
  destinations of the concept-list links (`?selected=<id>#detail-content`).

Live shell markup includes inert launcher and document viewbar/Sections
placeholders only when the server has attached Studio; index feedback also
requires an actual `index.md` document. Their final responsive size is reserved
before first paint only when the parser-blocking theme runtime has marked
JavaScript available. Inactive Studio sessions, documentless indexes, portable
and no-JS targets reserve no inactive Studio space. Studio fills those slots in
place (removing any feedback placeholder it cannot fill), while
`document-outline.js` owns the Sections control.
The index feedback placeholder carries both the comment and the read-only
layout; the theme runtime marks the session's comment capability from the
Studio bootstrap so the matching one is reserved. The live loading notice stays
visible until a successful boot, outside document flow and anchored below the
top bar, so settling boot does not displace authored content. If Studio cannot
boot, including when its module never runs, the notice joins the flow below the
bar and the unfilled placeholders collapse.
Custom templates without the placeholders keep the legacy client mount path.

## 2. Static asset overrides

Drop a file at `.okf-loom/viewer/static/<file>` to replace the built-in asset.
Recognised files: `wiki.css`, `shell.css`, `wiki.js`, `graph.css`, `graph.js`, `theme.js`.
Both the live server (`/__static/...`) and the static-site builder honour
overrides. `shell.css` is the replaceable application-shell layer: multi-file
templates load it after `wiki.css`, and single-file output inlines it between
`wiki.css` and `graph.css`. Override `shell.css` when changing shell layout or
current/focus styling; override `wiki.css` for the underlying viewer tokens and
page surfaces. Each file replacement wins at its documented cascade position.

```text
.okf-loom/viewer/static/wiki.css   ← replaces the bundled stylesheet
```

## 3. Retired type palette overrides

Type-colour overrides in `.okf-loom/viewer/palette.json` are **retired and
ignored by Ledger**. Content types use neutral labels and icons on every
built-in surface; graph lenses use a muted ramp derived from the active
appearance palette, not colours assigned to types.

Existing files need not be deleted. The loader may still merge validated
values into compatibility data under the active-code gate, but built-in
renderers do not interpolate them into inline styles or paint them. This
is not an appearance configuration mechanism: select Bone, Slate, Clay,
Plum or System through `config.json` (§4) or the Appearance control.
Custom executable templates, styles and plugins remain subject to §5.

## 4. Viewer config

`.okf-loom/viewer/config.json` controls display defaults. All keys optional:

| Key              | Type    | Default   | Effect                                         |
|------------------|---------|-----------|------------------------------------------------|
| `name`           | string  | bundle dir name | Display name for the viewer / browser title. |
| `default_layout` | string  | `"cose"`  | Initial Cytoscape layout: `cose` / `concentric` / `breadthfirst` / `circle` / `grid`. |
| `theme` | string | `"bone"` | `bone` / `slate` / `clay` / `plum` / `system`. Saved reader choice > configured choice > Bone. `theme.js` alone owns `data-theme` and `localStorage["okf-loom.appearance"]`; System resolves Bone/Slate from the OS before paint and follows changes live. Old light names → Bone, Technical/Swiss dark → Slate, Coffee dark → Clay, Purple dark → Plum, auto → System; v1 aliases still load. Retired contrast/border/density settings are ignored. |
| `cdn`            | bool    | `true`    | If `false`, omit the Cytoscape.js CDN `<script>` tags from the single-file viewer and the `/__graph` page (for fully offline packaging — supply your own copy in that case). Markdown is rendered server-side by okf-loom's own stdlib renderer, so there is no client-side markdown parser to gate. |

```json
{
  "name": "Acme OKF",
  "default_layout": "breadthfirst",
  "theme": "slate",
  "cdn": true
}
```

## 5. Plugin hook (entry-point loader; current spec §15)

A `ViewerPlugin` is any object exposing:

```python
class ViewerPlugin:
    def on_concept_render(self, concept: Concept, html: str) -> str: ...
    def on_index_render(self, html: str) -> str: ...   # optional
```

`on_concept_render(concept, html)` runs after a concept-page body is rendered
and returns possibly-modified HTML (e.g. to inject a widget, badges, or
telemetry). `on_index_render(html)` is the same hook for index pages and is
optional — plugins that omit it are silently skipped on index pages.

### Discovery: Python entry points

Plugins are discovered via the **`okf_loom.viewer_plugins`** entry-points
group. Register one in `pyproject.toml`:

```toml
[project.entry-points."okf_loom.viewer_plugins"]
badge = "my_pkg.viewer:BadgePlugin"      # a class — instantiated once
make = "my_pkg.viewer:make_plugin"       # or a factory returning an instance
```

Either form is accepted: an entry point may point at a class (instantiated
with no args) or a factory callable that returns a plugin instance. An
already-constructed instance is also accepted. Plugins load in entry-point
registration order.

### Active-code gate (current spec §14)

Plugins **execute arbitrary Python** (module import alone runs code), so they
run ONLY when **both** of the following are true:

1. The bundle's `okf-loom.config.yaml` declares:

   ```yaml
   viewer:
     allow_active_code: true
   ```

2. **The operator has explicitly consented** by setting the
   `OKF_LOOM_ALLOW_ACTIVE_CODE` environment variable to `1` / `true` / `yes`, OR
   by passing `--allow-active-code` to `scripts/okf-loom serve` / `scripts/okf-loom build`.

The effective gate is `bundle_cfg.allow_active_code AND operator_consent`.
The default for both is `false`.

**Why two layers (trust model).** A bundle is an untrusted artifact from the
consumer's point of view: it ships `.okf-loom/viewer/templates/*.html`,
`.okf-loom/viewer/static/*.js`, and (via entry points) plugin Python that all
execute on every page view. If `allow_active_code: true` in the bundle were
sufficient on its own, a bundle could ship a malicious template that strips
the CSP and injects arbitrary JS — and every visitor to `scripts/okf-loom serve` would
run it with no operator warning. The operator-consent layer makes the
decision explicit: the operator looks at the bundle, decides whether they
trust its override/plugin sources, and only then opens the gate. A bundle
with `allow_active_code: true` and no operator consent produces the
built-in (safe) viewer — overrides silently ignored, no warning printed
(the closed gate is the safe default).

When the effective gate is open, `scripts/okf-loom serve` and `scripts/okf-loom build` print a
prominent stderr warning:

```
WARNING: active code (viewer overrides + plugins) is ENABLED for bundle <root>.
WARNING: Only run this for bundles whose override/plugin sources you trust.
WARNING: A malicious override can execute arbitrary JavaScript in every visitor's browser.
```

The same gate governs **template overrides**, **static asset overrides**,
**type palette overrides**, and **plugin discovery** uniformly — see
`okf_loom.viewer.assets._overrides_allowed` /
`effective_allow_active_code`.

When the gate is closed, **no entry-point discovery happens at all** —
`scripts/okf-loom serve` and `scripts/okf-loom build` do not even import plugin modules.

### Failure handling

A plugin that raises during load OR during a hook call is **logged to stderr
and skipped** — the viewer keeps serving. A failing plugin never crashes the
page render; the worst case is that the page is served without that plugin's
contribution.

### Manual attachment (still supported)

For embedders that do not want to use entry points, attach a plugin instance
(or a `CompositeViewerPlugin`) directly to the server after construction:

```python
from http.server import ThreadingHTTPServer
from okf_loom import Bundle
from okf_loom.viewer import OKFWikiHandler, CompositeViewerPlugin

class BadgePlugin:
    def on_concept_render(self, concept, html):
        return html.replace("</body>",
            '<div class="badge">Internal</div></body>')

plugin = CompositeViewerPlugin([BadgePlugin()], allow_active_code=True)
httpd = ThreadingHTTPServer(("127.0.0.1", 8787), OKFWikiHandler)
httpd.bundle = Bundle.load("path/to/bundle")
httpd.plugin = plugin    # ← explicit; bypasses entry-point discovery
httpd.serve_forever()
```

`okf_loom.viewer.plugins.build_viewer_plugin(bundle_root)` is the helper
that performs entry-point discovery + active-code gating in one call; it is
what `scripts/okf-loom serve` and `scripts/okf-loom build` use by default.

---

## 6. Client-side extension API: `okfLoomStudio.register(kind, config)` (current spec §15)

The studio client (`viewer/static/studio.js`) exposes a small JavaScript
extension registry on `window.okfLoomStudio`. It mirrors the server-side
`ViewerPlugin` seam's intent (let a bundle or harness grow the UI without
forking core) but runs **fully client-side**, after the page has booted.
Unlike the bundle `.okf-loom/viewer/` overrides (sections 1 to 5, which replace
whole files), `register()` is **additive** — you hand it a config object and
the studio wires it into the existing chrome.

```js
// CSP-safe (script-src 'self'): load this from a .okf-loom/viewer/static/*.js
// override, or eval it from your harness after the studio boots. No inline
// handlers; the studio owns every addEventListener.
window.okfLoomStudio.register(kind, config);
```

### Kinds

| Kind                 | Status                      | What it does                                                                 |
|----------------------|-----------------------------|------------------------------------------------------------------------------|
| `panel`              | **Ships, wired**            | Adds a panel destination inside the consolidated Studio home.                 |
| `viewMode`           | **Ships, wired**            | Adds a button to the Rendered/Source/Split view switch on concept pages.      |
| `toolbar`            | **Reserved (forward-compat)**| Accepted + warned, not wired. Planned: a studio-bar action button.           |
| `graphDecorator`     | **Reserved (forward-compat)**| Accepted + warned, not wired. Planned: a per-Cytoscape-node style hook.      |
| `suggestionRenderer` | **Reserved (forward-compat)**| Accepted + warned, not wired. Planned: a custom renderer for `/__apply` previews. |

The three reserved kinds are accepted so an extension that targets a future
toolkit version does not throw today; `register()` logs a clear
`console.warn` naming the kind and stating it is not wired, so an author
discovers the gap immediately rather than debugging a silent no-op.

### `panel` (ships) — worked example

A panel joins the built-in **Comments**, **Changes**, and **Agent activity**
destinations. Its toggle is placed in the consolidated Studio home's **Work
and review** group. Activating it opens the shared rail/dialog, updates
`aria-expanded`, and exposes **Back to Studio** navigation; closing the shared
surface restores the persistent shell Studio launcher. The shared surface owns
modal focus trapping and Escape at compact widths while the desktop rail stays
non-modal. The render callback receives the live studio state.

```js
// .okf-loom/viewer/static/my-panel.js  (loaded after studio.js)
window.okfLoomStudio.register("panel", {
  id: "coverage",                 // unique; used as the open-panel key
  label: "Coverage",              // the studio-bar toggle button label
  // render(container, ctx) is called whenever the panel opens. Wipe
  // container and rebuild; Studio owns the rail/dialog chrome, compact modal
  // trap, Esc, return navigation, and the home toggle's aria-expanded.
  render(container, ctx) {
    container.innerHTML = "";
    const open = ctx.state.comments.filter(c => c.state === "open").length;
    container.textContent = "Open comments: " + open;
    // ctx.onLive("activity", fn) subscribes to the live event bus, so the
    // panel can update in real time as the agent acts.
  },
});
```

The built-in panels are registered the same way (see the `panels.comments`,
`panels.changes`, and the demo `agent-activity` registrations in
`studio.js`). The context object (`ctx`) exposes:

- `ctx.state` — the live studio state (view, conceptId, comments, events,
  presence, openPanel, filters). Treat it as read-only; mutate through the
  studio's own actions.
- `ctx.el(tag, attrs, children)` — the studio's element helper (sets
  attributes, including `dataset` and `style` objects).
- `ctx.toast(msg, opts)`, `ctx.currentConceptId`, `ctx.boot`, `ctx.tokenFetch`.
- `ctx.onLive(type, fn)` — subscribe to the live event bus
  (`window.okfLoomLive`) for `activity`, `changed`, `created`, `removed`,
  `comment`, `presence`, `conn`, `resync`, … (§7.3 / §12).

### `viewMode` (ships) — worked example

A view-mode extension adds a button to the Rendered/Source/Split switch on
concept pages. It is read-only by design (the studio's directing model is
"the user comments, the agent edits"; §1.4). The extension's `onActivate`
is responsible for any custom rendering.

```js
window.okfLoomStudio.register("viewMode", {
  id: "outline",
  label: "Outline",
  onActivate(ctx) {
    // Build an outline from the headings and swap it into the view area.
    // The studio marks only this button pressed (aria-pressed) and clears
    // the built-in trio; deep-linking via ?view= stays on the built-ins.
  },
});
```

### Reserved kinds (forward-compat)

`toolbar`, `graphDecorator`, and `suggestionRenderer` are reserved for a
future release. They are documented here so the intended surface is
discoverable, but the studio does not yet call them. If you register one
today, the studio:

1. Accepts the call (does not throw) so an extension written against a
   future version degrades gracefully today.
2. Logs `console.warn("[okf-studio] register('<kind>') is reserved for a
   future release and is not wired in this version.")`.
3. Returns the config object unchanged.

Track wiring against the current spec; do not depend on reserved kinds until they are documented as current.

### When to use `register()` vs the `.okf-loom/viewer/` overrides

- **Replace a whole template or stylesheet** (e.g. a custom concept-page
  footer, a recoloured wiki.css): use sections 1 to 4 (file overrides).
  Those are the coarser-grained, replace-the-file mechanisms.
- **Add a panel / view mode / live-driven widget without forking core**:
  use `register()`. It is additive and survives toolkit upgrades because
  it depends only on the documented `ctx` shape.
- **Run server-side Python during render** (e.g. inject a badge into every
  concept page at build time): use the `ViewerPlugin` hook (section 5).

All three mechanisms compose: a bundle can ship a custom `wiki.css`, a
`ViewerPlugin`, and a `register("panel", …)` extension at the same time.

## 7. Live graph acquisition seam

Live, static and SPA graph pages load their external classic vendor scripts
with `defer`, in dependency order: Cytoscape → layout-base → cose-base →
cytoscape-fcose → dagre → cytoscape-dagre → graph.js. This applies to CDN and
verified offline assets alike; CDN integrity and crossorigin pins are unchanged.
Overrides must not rely on these globals during parsing. `theme.js` remains
parser-blocking for first-paint appearance. Single-file output retains its
parser-blocking vendor chain ahead of its inline graph runtime.

On live graph pages, `window.okfGraphData` is a promise for the full
`/__data/graph.json` projection. Whichever of graph.js or studio.js needs it
first creates it; the other reuses it. Failed initial acquisition clears the
promise for retry; pagehide clears it and fences late Studio completions.
An accepted initial acquisition also emits the event below, so a BFCache
restart reconciles Studio without a second request. This is an acquisition
seam, not a mutable graph-state API;
Studio does not depend on the diagnostic `window.__okfLoomGraph` hook.

Graph.js retains ownership of refresh aborts, deadlines and latest-wins
admission. Only an accepted refresh replaces the promise and emits
`okf-loom:graph-data` with the accepted projection as `event.detail`; Studio
reconciles that event instead of issuing another full request. Non-graph live
pages use `?view=structure` (no bodies or reserved-document HTML) when visible
connection/comment chips or index recency need it, or lazily when the command
palette opens. Accepted structural live updates refresh those consumers.
Pagehide fences pending Studio acquisitions; persisted pageshow reacquires
structure for those consumers under the same lazy refresh rules, while graph.js
retains ownership of full graph-page acquisition.

Portable viewers retain their existing transport: single-file uses embedded
`window.BUNDLE`; static/SPA graph pages read their emitted graph JSON. This
seam adds no assets, origins, CSP permissions or portable requests.

Single-file serialization stores each retrieval chunk's `canonical_chunk` once,
without a second serialized `payload`. Before viewer scripts run, the bootstrap
provides the existing enumerable `window.BUNDLE.retrieval_corpus.chunks[i].payload`
property as a lazy JSON parse, cached on first access as an ordinary mutable
property. Assignment before first access is also supported. Overrides reading
`window.BUNDLE` see the same shape and exact values; the literal embedded JSON
is a private transport, not the runtime API. Canonical bytes, hashes, strict
retrieval validation, rendered `bodies`, CSP and static/SPA exports are unchanged.
Custom single-file templates that differ from the built-in template retain the
original serialized `payload` as well, so older bootstraps remain compatible;
they do not receive this size reduction.

Ledger fonts (Public Sans and Azeret Mono, normal/italic) are verified vendored
assets, not external network dependencies. For live, static and SPA pages,
`ledger-fonts.css` references relative WOFF2 files under
`__static/ledger-public-sans/` and `__static/ledger-azeret-mono/`; static/SPA
builds copy the faces and SIL OFL `LICENSE` files into those directories even
in CDN mode. Publish both directories with the site and permit `font-src 'self'`
in a compatible CSP. The stylesheet also carries the full OFL notices.

Only single-file output embeds all four faces as base64 data URLs and needs
`font-src data:` for Ledger. All targets use `font-display: swap`, so text can
paint in a fallback face before the fonts arrive. Only serve and SPA preload
the two upright faces; static pages omit font preloads to avoid CORS errors
when opened via `file://`. Do not widen script policy.
