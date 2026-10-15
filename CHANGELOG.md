# Changelog

All notable changes to okf-loom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and product versions
follow [Semantic Versioning](https://semver.org/). The product version
(okf-loom 2.0.0) is separate from the OKF format version it implements
(OKF 0.2).

## [Unreleased]

### Fixed

- Live index cards, type/count statistics and document Connections now refresh
  in other open tabs after watcher or CLI edits, without reloading the page or
  replacing reader controls and comment drafts; read-only viewers stay live too.

- On touch screens the code-block Copy button no longer covers the first
  line of code, and stays in view while a long line is scrolled sideways.
- Graph JSON tolerates unknown query parameters (including cache-busters);
  only `view` is interpreted: absent means full, one `structure` means body-free,
  and other or repeated values return `400 invalid_request`. The route reference
  now records both views and graph/static final-byte ETag revalidation.
- Graph first frames avoid redundant default-URL restyling and a discarded grid
  layout; initial lens/theme updates are batched without changing settled positions.
- Graph canvas labels remeasure and redraw once when Ledger fonts load late,
  without delaying the first frame or rerunning the layout.
- Live pages reserve inert Studio launcher and document viewbar geometry only
  when Studio is attached, and index feedback only for an actual index document.
  Studio fills those slots in place (removing feedback it cannot fill); its
  truthful loading/failure notice stays anchored below the bar while booting.
  Inactive sessions, documentless indexes, portable and no-JS pages reserve no
  inactive Studio chrome.
- Split view now clears the "Loading source…" notice whenever the source pane
  is populated, including by a concurrent document or live-update load. A
  redundant request no longer leaves a loading or failure notice above source
  that has already loaded.
- A v1 `presence.json` in the Studio session no longer stops `serve`. It is
  normalised to the v2 format (unknown actors become `agent`, unknown states
  `idle`) instead of being rejected with a bare `error:`.
- A presence error caused by the stored file now names that file and says to
  stop the Studio and delete it (presence is ephemeral); a rejected update
  names the allowed actor and state values.
- `comment-claim --actor <name>` now flips presence to `editing` as documented.
  Presence records a role, so it silently failed for any name other than
  `agent`/`user`; the name stays on the comment as `claimed_by`. `presence
  --actor <name>` likewise shows a v1-style custom name as the agent.
- On phones, the graph route now follows an in-place resize or rotation
  (390 → 320 wide, for example). Before, the top bar kept the old width and the
  page stayed zoomed out until reload.
- The Split view tab now lines up with Rendered and Source on touch screens.
  It sat about 4px higher.
- The graph's selects (Graph settings Depth and Group by, and the focus bar's
  neighbourhood depth) now use the Ledger field style: square 1px outline,
  palette colours, Public Sans, focus ring, 44px touch target and a
  forced-colours outline. Before, they used the browser default.
- The "Live Studio is unavailable" boot notice now sits just below the top
  bar while Studio loads. Before, it covered the brand, search and
  destinations (or, on narrow graph pages, was hidden under the bar).
- If Studio cannot load at all (for example, a blocked script), the page now
  reports the failure. The notice then sits in the page below the bar, and the
  empty Studio launcher, view bar and feedback slots are removed.
- Read-only and public read-only sessions reserve the read-only page-feedback
  layout (View comments plus its note), so the index no longer shifts when
  Studio loads.
- On tablets and in phone landscape (641–900px wide), the graph pane tabs
  are now Ledger tabs with a visible current pane. Before, they were
  unstyled browser buttons.
- On narrow screens, the single-file Index no longer shows the
  Graph/Concepts/Details pane tabs, which controlled the hidden graph. They
  appear with the graph, and the pressed tab matches the visible pane.
- The favicon is now a square Ledger mark (ink tile, bone bars, warm 1px frame)
  instead of the old rounded teal tile. It is the same inline icon in every
  output, and the live 404 page now has it too.

### Changed

- Live static assets now use final-byte strong ETags and body-free conditional
  GET/HEAD responses. `Cache-Control: private, no-cache` requires revalidation on
  every reuse, so toolkit edits, overrides and generated font CSS stay fresh.
- Graph pages defer the ordered, integrity-pinned vendor chain in CDN and
  offline modes, letting the appearance runtime and page styles load without
  waiting for graph dependencies. Single-file vendor ordering is unchanged.
- Live document pages, document data and search reuse bounded, content-keyed
  heading render work without changing canonical-source or section-link checks.
- Live graph reads reuse single-flight, generation-owned rendered bodies while
  recomputing clock-sensitive metadata; index/graph HTML skip unused bodies.
  Graph JSON now has final-byte strong
  ETags and revalidates with `private, no-cache` (no shared-cache storage);
  Studio shares the graph page's initial
  request and uses body-free structure only for visible badges/recency or an
  opened command palette on other pages.
- **Ledger appearance:** Bone (default, light), Slate, Clay, Plum (dark), plus
  System (Bone/Slate following the OS). Square geometry, ruled inventory,
  neutral content types and palette-derived graph lenses replace the family
  system. The page maximum is 1280px, reading column 760px, prose 55ch even in
  full-width Focus. Contrast, border and density modifiers are removed.
- Public Sans and Azeret Mono normal/italic faces are self-hosted on every
  output, with verified hashes and full SIL OFL notices. Live/static/SPA pages
  use relative WOFF2 files; static/SPA builds (including CDN mode) emit
  `__static/ledger-public-sans/` and `__static/ledger-azeret-mono/` with the faces
  and OFL `LICENSE` files. Publish both directories with the site. Single-file
  output alone embeds the faces as base64 data URLs. All use `font-display: swap`
  (text may paint in a fallback face first); only serve/SPA preload the upright
  faces. No external font network access is needed on any target.

### Appearance migration

| Old name | Ledger choice |
|---|---|
| `technical-light`, `swiss-light`, `purple-light`, `coffee-light` | `bone` |
| `technical-dark`, `swiss-dark` | `slate` |
| `coffee-dark` | `clay` |
| `purple-dark` | `plum` |
| `auto` | `system` |
| v1 `light`, `pastel`, `sepia` (via their previous light aliases) | `bone` |
| v1 `dark`, `midnight` (via Technical dark) | `slate` |

Old config values still load; unknown Studio values fail closed. Browser legacy
keys are read only when `okf-loom.appearance` is absent. A saved choice wins over
configuration, then Bone; configuration is not persisted. Reset clears legacy
appearance choices too, so they cannot reappear on reload. Retired modifier keys
are ignored. Type-colour overrides in `.okf-loom/viewer/palette.json` are
retired and ignored by Ledger: types remain neutral and graph lenses use the
active palette's muted ramp. Existing files need not be deleted; compatibility
payloads may still contain their values, but the built-in viewer does not paint
them. The public JS API is `setTheme`, `getChoice`, `getResolvedTheme`,
`getScheme`, `reset` and `on`; old family/mode/modifier setters are removed.
`okf-loom:themeChanged` stays on window and now represents palette changes,
including dark-to-dark changes, with current/previous palette and scheme.
README media refresh is a separate follow-up; no product version bump here.

## [2.0.0] - 2026-09-24

The first release of okf-loom v2, maintained by SpatialNexus as a fork of
[okf-loom v1.0.0](https://github.com/ojamin/okf-loom) by ojamin. It includes
the features ojamin added after v1.0.0; they are marked *(ojamin)* below.

### Highlights

- **OKF 0.2.** Optional provenance, lifecycle and Attested Computation
  metadata are validated and shown as advisory badges, and `upgrade` migrates
  OKF 0.1 bundles conservatively.
- **Annotations v2.** Exact comments are re-checked against the current text
  and report their health instead of drifting; **Re-anchor** moves them
  explicitly.
- **Evidence-first retrieval.** Search results carry the matching passage, a
  line-range citation and ranking reasons, and the new `context` command
  builds deterministic, citation-ready Context Packs within a token budget.
  Static, SPA and single-file builds run the same retrieval in the browser.
- **Safer sharing.** Public tunnel links are read-only by default, with
  separate opt-ins for commenting and editing.
- **Feedback from exports.** Readers of a static or single-file export can
  write a page-level feedback note that you import into your local Studio.
- **New appearance.** Four theme families, each light and dark, with contrast,
  border and density settings.
- **Graph.** Lenses grouped in one panel, **Find in graph**, full screen, a
  keyboard path trace, a short tour and the `graph-quality` report.

### Added

#### Format and migration

- OKF 0.2 validation for the optional provenance (`generated`, `verified`,
  `sources`), lifecycle (`status`, `stale_after`) and Attested Computation
  families. `type` remains the only required key. Trust metadata is advisory
  and never authorization, and computations are never executed.
- `upgrade --preview` (the default, writes nothing), `--actor`,
  `--expected-revision` and `--format`. With `--actor`, `--apply` adds a
  `generated: {by, at}` mapping from a concept's valid legacy timestamp, which
  is kept byte for byte. A declared 0.1 root index becomes 0.2, and the
  declaration is written last. See
  [Migrate between spec versions](docs-bundle/how-to/migrate_spec_version.md).
- `write-concept --no-defaults`, which skips the automatic `resource` and
  `generated` values on create.

#### Commands

- `context`: deterministic, citation-ready Context Packs within a token
  budget (`--budget-tokens`, `--neighbors`, `--format json|markdown`), also
  served at `GET /__context`. See
  [Agent Context Packs](docs-bundle/reference/context_packs.md).
- `graph-quality`: advisory graph-health signals for a bundle *(ojamin)*.
- `update-section` and `replace-text`: block-level edits that fail closed on
  a missing or ambiguous target *(ojamin)*.
- `comment-reply`: reply in a comment thread without resolving it *(ojamin)*.
- `tunnel`: attach, detach or report a public tunnel on a running Studio
  without restarting it *(ojamin)*.
- `proposal` (`create`, `list`, `show`, `decide`, `withdraw`, `apply`,
  `undo`): experimental proposal review, activated with
  `serve --enable-proposal-review` and reported by
  `capabilities --enable-proposal-review`. The Studio Proposals panel is
  opt-in and inspection-only; direct editing stays the default.

#### Studio and comments

- Annotations v2: exact annotations resolve against the current text and
  report `resolved`, `stale`, `ambiguous` or `invalid` health. **Re-anchor**
  moves a comment to new wording, and the composer says whether a comment is
  an exact annotation or a page comment.
- **Comment on this page** for whole-page comments.
- `serve --allow-tunnel-comment` (public comments and replies only) and
  `--allow-tunnel-edit` (public comments and edits); `tunnel` accepts both.
- Portable page-level feedback: `build` and `render`
  `--include-portable-feedback` add an offline note composer to static and
  single-file exports. Readers download a `.okf-feedback.json` note, which
  you import with **Import feedback note** (`/__feedback-import`) in a local,
  editable Studio. Import is refused while a tunnel is attached. See
  [Import a portable feedback note](docs-bundle/how-to/import_portable_feedback.md).
- `viewer.review_base_url` config key: exports show a **Review in Studio** link
  to the Studio you name.
- `build` and `render --include-comment-summary`: embed sanitized
  active-comment counts in static and single-file exports.
- `comment-list --document` and `presence --message`.
- Link previews show the target's title and type, from the new additive
  `GET /__data/summary?id=` route.
- Bundle-local images, video and PDFs are served by the Studio and copied into
  static and SPA builds, and `validate` warns about missing images *(ojamin)*.

#### Search and discovery

- Search results show the exact matching passage with a `[path.md#Lx-Ly]`
  citation and up to eight ranking reasons. `search --entity` filters by
  entity.
- Portable retrieval: static, SPA and single-file builds run all six search
  modes and build Context Packs in the browser from a content-addressed,
  revision-bound passage corpus that fails closed if it is incomplete or
  tampered with.
- `discover --min-confidence` and `--include-low-confidence`, suggestions
  grouped by how safe they are to apply, and the `discover.suppress_pairs`
  and `discover.suppress_phrases` config keys *(ojamin)*.

#### Graph

- An **Explore with lenses** panel for the six lenses.
- **Find in graph** by title, id or tag; a full-screen control; and
  **Graph settings** grouping spacing, layout and relationship-label options.
- Keyboard path tracing between two concepts (Shift+Space) and an optional
  45-second graph tour.
- Metadata-based graph grouping *(ojamin)*.

#### Appearance

- Four theme families (Technical, the default, plus Swiss, Purple and
  Coffee), each light and dark, with Auto following the system setting.
- Soft contrast, interface borders (off by default) and compact (default) or
  comfortable density.
- Light themes use softened surfaces rather than paper white; code blocks stay
  light in light mode, and printing stays white.

#### Builds and distribution

- `build` and `render --assets offline`: ship byte-verified copies of the
  pinned Mermaid, KaTeX, highlight.js and Cytoscape.js libraries with their
  licences and notices, so no CDN is needed. A single-file offline build works
  straight from disk.
- `build --output-policy managed` (static and SPA) and `--adopt-output`:
  record the files a build wrote and refuse to replace anything it did not
  produce.
- `scripts/build_skill_archive.py --revision`, `--verify ZIP` and
  `--verification {reproducible,integrity}`.
- Maintainer scripts that regenerate the README media:
  `scripts/capture_readme_media.py` and `scripts/record_showcase_media.py`.
  See [CONTRIBUTING.md](CONTRIBUTING.md).

### Changed

- okf-loom implements OKF 0.2; `--version` prints
  `okf-loom 2.0.0 (SPEC v0.2)`. Python 3.11 or newer and zero runtime
  dependencies are unchanged.
- The graph's Focus lens is now called **Explore neighborhood**.
- `write-concept` fills in `generated` (this tool and the current UTC time)
  when it creates a concept, alongside `resource`. Use `--no-defaults` to
  skip both.
- Generated index pages use bundle-absolute links *(ojamin)*.

### Breaking changes

- **`studio.theme`** accepts only `auto` and the eight family-mode names:
  `technical-light`, `technical-dark`, `swiss-light`, `swiss-dark`,
  `purple-light`, `purple-dark`, `coffee-light` and `coffee-dark`. The v1
  values `light`, `dark`, `pastel`, `sepia` and `midnight` are rejected as a
  configuration error. (They load again as aliases; see
  [Unreleased](#unreleased).)
- **Tunnels are read-only by default.** In v1 the tunnel host was trusted like
  the local Studio, so anyone with the link could comment and apply changes.
  In v2 the tunnel is served by a separate public listener that can only read
  unless you pass `--allow-tunnel-comment` or `--allow-tunnel-edit`.
- **The skill archive is built from a commit.** `build_skill_archive.py` used
  to zip the working tree; it now captures `--revision` (default `HEAD`), so
  uncommitted changes are left out. `--out` and `--verify` cannot be combined.

### Migration notes

- **Bundles.** Preview first, then apply what you reviewed:

  ```bash
  scripts/okf-loom upgrade path/to/bundle --preview --format json
  scripts/okf-loom upgrade path/to/bundle --apply --expected-revision <revision-from-preview>
  ```

  Add `--actor <who produced the content>` to both commands only if you know
  the original producer. `--apply` needs Linux (`renameat2` atomic exchange)
  and fails closed elsewhere. Back up the bundle before applying.
- **Theme config.** Replace a v1 `studio.theme` value with `auto` or a
  family-mode name. The Studio's own migration of saved browser choices is a
  good guide: `light` → `technical-light`, `dark` → `technical-dark`,
  `pastel` and `sepia` → `swiss-light`, `midnight` → `technical-dark`. Readers'
  saved choices in the browser are migrated automatically.
- **Tunnels.** To keep v1's behaviour, where anyone with the link can comment
  and edit, add `--allow-tunnel-edit`. For comments only, use
  `--allow-tunnel-comment`.
- **Skill archives.** Commit your changes before running
  `scripts/build_skill_archive.py`, or pass `--revision` to pick a commit.

## [1.0.0] - 2026-07-03

The first release, by ojamin: the loadable skill layout, the live Studio and
its comment loop, six search modes, the graph lenses, the command palette,
presence, the agent activity, pending-changes and group-undo panels, `--tunnel`
sharing and the skill archive builder. See
[ojamin/okf-loom](https://github.com/ojamin/okf-loom) for its README and
history.

[Unreleased]: https://github.com/SpatialNexus/okf-loom-v2
[2.0.0]: https://github.com/SpatialNexus/okf-loom-v2
[1.0.0]: https://github.com/ojamin/okf-loom
