# okf-loom v2 desktop UI/UX audit and action plan

> **Historical scope:** findings describe the dated revision and observed
> surfaces below, not a fresh audit. The [maintenance plan](../../docs-bundle/explanation/maintenance_plan.md)
> owns current priorities and the [spec](../../docs-bundle/reference/spec.md)
> owns current behavior. Unverified accessibility and release claims remain
> unverified; this action plan does not gate unrelated approved maintenance.

Audit date: 2026-08-11  
Audited branch: `ui-patch`  
Audited revision: `04242fd`  
Primary surfaces: live Studio index, concept reader, Search/Context, graph, Studio controls, Appearance  
Desktop targets: 1440×900, 1280×800, and 720×450 as a 200%-equivalent reflow check

## Executive summary

okf-loom v2 has crossed the line from “rendered Markdown plus utilities” into a credible editorial workbench. Its strongest surface—the concept reader—communicates structured knowledge, provenance, relations, and agent collaboration in one coherent page. Search returns unusually trustworthy evidence. The graph has real analytical depth rather than being decorative. The tokenized visual system, responsive behavior, and output-parity discipline are solid foundations.

The observed default-state surfaces expose broad capability, but capability completeness remains unverified because mutation, alternate-output, failure-state, and assistive-technology workflows were not all exercised. Within the audited default states, the dominant problem is **too much capability competing at once**.

Three changes would produce the largest improvement:

1. **Establish one stable navigation hierarchy.** Separate destination navigation (`Index`, `Search`, `Graph`) from Studio/session state (`Watching`, `Comments`, `Changes`, activity) and task actions (`Commands`, view modes, Appearance).
2. **Progressively disclose evidence and controls.** Make index cards, Search results, graph details, and long concept-page utilities concise by default, with full evidence one deliberate action away.
3. **Reduce keyboard and reading distance.** A 41,967px concept page exposes 260 visible focusability candidates, and the dense docs graph exposes 251. These are risk indicators—not verified sequential Tab counts—and they justify focused keyboard and assistive-technology testing before redesign.

No P0 failure was visible in the captured default states. The evidence is insufficient to rule out P0 failures in unexercised workflows. The current release should be treated as **capability-rich but not yet interaction-efficient or accessibility-complete**. The recommended plan starts with navigation semantics, graph accessibility, and disclosure—not a visual rewrite.

## Product and user model used for this audit

No repository-level `PRODUCT.md` or `DESIGN.md` was present. The audit therefore derives its product model from `README.md`, `SKILL.md`, the current specification, current design plans, and the live UI.

### Product thesis

okf-loom is an evidence-led editorial workbench and living atlas for plain-Markdown knowledge trees. It should help a person:

1. orient in an unfamiliar bundle;
2. find and read the right concept;
3. understand how concepts connect;
4. collect bounded, citation-ready context;
5. direct an agent by commenting on exact text;
6. review changes and activity without leaving the knowledge surface.

### Primary users

- **Knowledge maintainer:** authors, reviews, links, and improves concepts.
- **Technical reader:** searches, navigates, inspects evidence, and copies context.
- **Agent director:** selects text, leaves instructions, monitors activity, and reviews attributed changes.

### Experience standard

The product should feel like a calm reading environment first, an analytical workbench second, and an operational console only when the user opens Studio controls. Expert capability should remain available without imposing expert density on every first view.

## Scope and method

### Evidence inspected

- Live docs Studio: index, `/reference/spec`, Search for `tunnel`, and graph.
- Live demo Studio: index, `/tables/orders`, Search for `orders`, and graph.
- Fresh screenshots and DOM/focus/landmark data captured for this audit (`ui-ux-audit-2026-08-11`).
- Historical canonical captures for `348e66f` (`ui-overhaul-final`); they are not proof for `893028d` or later changes.
- Both evidence sets were removed from the public tree before publication and remain only in the repository's Git history (see the Evidence index).
- Product and implementation contracts in `README.md`, `SKILL.md`, `docs-bundle/reference/spec.md`, and `docs/design/ui-overhaul-release-matrices.md`.
- Relevant templates and styles under `scripts/okf_loom/viewer/` plus shared composition in `scripts/okf_loom/render.py`.

### Fresh capture summary

| Scenario | HTTP | Page height | Focusable controls | Horizontal overflow | Console |
|---|---:|---:|---:|---|---|
| Docs index, 1440 | 200 | 6,598px | 100 | None | Clean |
| Docs concept, 1440 | 200 | 41,967px | 260 | None | Clean |
| Docs Search, 1440 | 200 | 5,657px | 44 | None | Clean |
| Docs graph, 1440 | 200 | 900px | 251 | None | Cytoscape wheel-sensitivity warning |
| Demo index, 1440 | 200 | 2,142px | 29 | None | Clean |
| Demo concept, 1440 | 200 | 1,785px | 60 | None | Clean |
| Demo Search, 1440 | 200 | 2,622px | 44 | None | Clean |
| Demo graph, 1440 | 200 | 900px | 76 | None | Cytoscape wheel-sensitivity warning |
| Demo concept, 1280 | 200 | 1,843px | 60 | None | Clean |
| Demo graph, 1280 | 200 | 800px | 76 | None | Cytoscape wheel-sensitivity warning |
| Demo concept, 200%-equivalent | 200 | 2,661px | 62 | None | Clean |

### Evidence labels

- **Observed:** visible in a fresh screenshot or directly represented in captured DOM data.
- **Contract-backed:** specified and covered by existing browser/release matrices.
- **Expert review:** heuristic or accessibility review, not a user study.
- **Needs moderated validation:** the expected impact is credible but should be measured with representative users.

### Limitations

- This is not a legal WCAG certification.
- No moderated usability study was conducted.
- No full screen-reader session was completed during the initial pass.
- Copy, comment, edit, tunnel, and undo paths were reviewed non-destructively; persistent mutations were not submitted.
- The 720×450 capture approximates the CSS layout width of a 1440px viewport at 200%; it is not an actual browser-zoom test.
- Fresh captures use headless Chromium, Technical Light, and forced reduced motion. Historical `348e66f` evidence covers other themes and an actual 400% capture for that revision, but this document does not freshly re-score every theme or state.
- DOM “focusable controls” are visible focusability candidates selected by the capture script, not proof that every candidate is a sequential Tab stop.
- Canonical captures were produced from source revision `348e66f` and published by `04242fd`; no runtime files changed between those revisions.
- Fresh default-state evidence was captured with hashes and capture metadata; it is no longer in the public tree (see the Evidence index). That evidence remains the broader historical baseline for `348e66f`, not current-candidate proof.

### Functional journey coverage matrix

This matrix separates capability presence from behavior actually exercised in the fresh audit. Existing release tests are useful contract evidence, but they are not substituted for a current end-to-end audit result.

| Journey | Fresh actions exercised | Fresh result | Contract evidence | Status / remaining states |
|---|---|---|---|---|
| Load and orient from index | Opened docs/demo index; inspected landmarks, headings, controls, dimensions, and default content | Both loaded with HTTP 200; no page-level overflow | Viewer/browser suites and canonical index captures | **Observed default state**; novice browse task not moderated |
| Open and read a concept | Opened docs spec and demo Orders; inspected structure and reflow | Both loaded; article/header/connections visible | Concept renderer and browser suites | **Observed default state**; history, scroll preservation, and source transitions pending live journey result |
| Search | Opened query-result URLs for `tunnel` and `orders`; inspected default result content | Both loaded; result evidence visible | Retrieval/search release tests | **Observed default state**; mode changes, filters, no-result, copy, and error recovery pending live journey result |
| Build/copy context | Inspected available actions and result text | Controls present | Context Pack contract and browser tests | **Not freshly exercised**; clipboard success/failure and pack scope need live proof |
| Rendered/Source/Split | Inspected control presence where available | Controls present | Keyboard/release matrix | **Not established by initial capture**; transition, persistence, and focus need live proof |
| Select and comment | Inspected comments/Studio entry points non-destructively | Capability visible | Annotation browser/release evidence | **Not freshly submitted**; composer, cancel, validation, persisted comment, and public authority need journey proof |
| Comments/Changes/Activity/Commands | Inspected default zero/idle controls | Controls and counts visible | Studio browser suites | **Default state only**; non-empty, undo, failure, and reconnect states untested in fresh capture |
| Graph exploration | Opened small/dense graph; inspected default tour, canvas, lists, lenses, and controls | Both loaded and reported ready | Graph browser and semantic-readiness suites | **Observed default state**; lens, selection, path, reset, keyboard equivalence, and state restoration pending live journey result |
| Appearance | Inspected trigger and current theme state | Technical Light reported | Theme contract and release matrix | **Default state only**; open/change/reset/deny-storage behavior not freshly exercised |
| Sharing/authority | Reviewed visible status plus README/SKILL contract | Security model documented | Tunnel/security tests | **Contract-backed, not user-tested**; public read/edit mental model needs moderated validation |

### Expected feature and desktop-convention matrix

| Expected convention | Current implementation | Assessment | User impact / recommendation |
|---|---|---|---|
| Stable primary navigation and current location | Index, Search, Graph, Appearance, and Studio controls are visible but mixed across one/two rows | **Present, inconsistent hierarchy** | Adopt F01 shell and `aria-current`; maintainer approval required for Studio consolidation |
| Search recovery and query persistence | Query form, modes, filters, results, and direct URL exist | **Present; recovery states incompletely audited** | Preserve URL state; add clear no-result and invalid-filter recovery proof |
| Help and shortcut discovery | Commands exposes `Ctrl+K`; graph has a tour; labels mention some shortcuts | **Partial** | Add one conventional Help/shortcuts entry; keep command aliases searchable |
| Back/history and state restoration | Browser navigation and route URLs exist | **Unverified in current journey evidence** | Test scroll, query, lens, selection, panel, and view-mode restoration |
| Undo/history for edits | Changes and undo are product contracts | **Present, default state observed only** | Test non-empty grouped changes, destructive boundaries, and failure recovery |
| Recent/saved entry points | Recent graph lens and activity exist; index prioritization is release/editorial content | **Partial** | Add role-based Start here before adding personalization or saved views |
| Loading, empty, error, and offline/reconnect states | Typed fallback contracts exist across Studio/retrieval/graph | **Contract-backed; not comprehensively visually audited** | Add state fixture matrix before claiming completeness |
| Sharing and authority clarity | Secure local/public/edit model is documented; UI status is compact | **Present, operator-centric** | Explain authority in Studio without exposing tokens or widening permissions |
| Settings and reset | Appearance supports families, modes, modifiers, and reset | **Present; default state only in fresh capture** | Keep conventional utility placement and verify checked/reset/focus behavior |
| Alternate outputs | Live/static/SPA/single-file parity is a core contract | **Contract-backed, outside fresh desktop-state capture** | Preserve honest capability/fallback notices through every shell change |
| Keyboard-equivalent operation | Skip links, labels, focus styles, graph lists, and release matrices exist | **Partial / current audit incomplete** | Complete the keyboard-focus matrix below; avoid flat hundreds-candidate paths |
| Accessible help for unfamiliar domain terms | Context Pack, lens, evidence, Focus, and Studio terms appear | **Partial** | Add concise plain-language subtitles and one glossary/help route; do not dilute expert detail |

### Route × viewport × state coverage

| Surface | 1440 default | 1280 default | 720 reflow proxy | Actual 200%/400% | Dark/themes | Interaction/error states |
|---|---|---|---|---|---|---|
| Index | Docs + demo | Not fresh | Not fresh | Canonical 400% only | Canonical release set | Not fresh |
| Concept | Docs + demo | Demo | Demo | Canonical 400% only | Canonical release set | Partial canonical; fresh journey pending |
| Search | Docs + demo | Not fresh | Not fresh | Not fresh | Canonical representative states | Default results only |
| Graph | Docs + demo | Demo | Not fresh | Canonical 400% only | Canonical release set | Default tour only in fresh capture |
| Studio overlays | Controls visible | Indirectly visible | Concept shell visible | Canonical representative states | Canonical release set | Fresh non-empty/error paths pending |
| Appearance | Trigger/current value visible | Trigger visible | Trigger visible | Canonical representative states | Canonical release set | Fresh change/reset journey pending |
| Source/Split | Controls visible on applicable concept | Visible | Reflowed shell | Canonical representative states | Canonical release set | Fresh transition/persistence pending |

### Accessibility evidence and keyboard-focus matrix

| Area / command | Initial fresh evidence | Status | Decisive next proof |
|---|---|---|---|
| Skip link and route entry | `Skip to content` is first captured focus destination | **Observed** | Activate on every route; verify target and focus/scroll |
| Tab / Shift+Tab | First 18 destinations captured; candidate counts available | **Partial** | Complete bounded journey, reverse order, no traps, no hidden candidates |
| Enter / Space | Roles and controls inspected | **Not established initially** | Activate nav, disclosures, modes, lens/list items, and comment controls |
| Escape and overlay ownership | Release matrix specifies behavior | **Contract-backed** | Live Appearance, Studio, comment, command, and graph-tour restoration trace |
| Accessible names/roles/states | DOM text/ARIA candidate data captured | **Partial** | Browser accessibility-tree assertions and screen-reader confirmation, especially F05 |
| Status announcements | Live/status elements inspected conceptually | **Partial** | Screen-reader verification for copy, selection, lens, comments, errors, and reconnect |
| Graph canvas/list equivalence | Canvas plus concept/relationship controls present | **Partial / risk** | Keyboard and screen-reader select, inspect relation/path, and open concept |
| Reflow | No horizontal overflow at 720 CSS px | **Observed proxy** | Actual 200% and 400% browser zoom with task-equivalence checks |
| Contrast/non-color cues | Token/release evidence exists | **Contract-backed baseline** | Fresh forced-colors, grayscale, focus, selected, error, and disabled-state review |
| Target size/reduced motion | Reduced motion forced in fresh capture; geometry available | **Partial** | Default/reduced-motion pair and computed target-size assertions |

### Visual quality matrix

| Surface | Hierarchy | Density/scan | Typography/measure | Controls/states | Main visual risk |
|---|---|---|---|---|---|
| Index | Strong hero/type grouping | Docs inventory is very long and repeated | Editorial voice is strong | Many equal-weight entry points | Dashboard and hand-authored directory compete |
| Concept | Strongest hierarchy and anatomy | Good on demo; extreme docs spec length | Strong serif/sans/mono roles and measure | Utilities multiply with content | Long-document orientation and traversal |
| Search | Query/result hierarchy is clear | Expanded evidence slows comparison | Evidence text is readable but dense | Repeated copy/context/disclosure actions | Five results require 5,657px in docs fixture |
| Graph | Canvas/detail split is conceptually strong | Dense docs fixture truncates/crosses; small demo reads better | Labels are compact and sometimes truncated | Tour/lens/filter/view controls compete | First-read legibility and onboarding obstruction |
| Reflow | Header and article recompose cleanly | Secondary groups move later | Text remains readable | `On this page` stays near header | Actual zoom and interaction states remain unverified |
| Themes/states | Canonical release set shows broad parity | Not freshly re-scored | Token system is coherent | Hover/focus/pressed/loading/error not all fresh | Avoid theme-wide conclusions from Technical Light alone |

## Experience scorecard

Scores are directional (1 = poor, 5 = excellent), intended to prioritize work rather than certify quality.

| Dimension | Score | Assessment |
|---|---:|---|
| Default-state rendering | 4.5 | **Observed:** audited routes loaded, rendered, and reflowed cleanly in the captured states. |
| Capability discoverability | 3.0 | **Observed + expert review:** broad capability is present, but navigation, vocabulary, and disclosure make it harder to find. |
| Exercised workflow correctness | Not scored | Important interactive, persistence, failure, alternate-output, and assistive-technology workflows need the matrices below before a completeness score is defensible. |
| Information trust | 4.5 | **Observed + contract-backed:** exact excerpts, citations, source lines, provenance, relation semantics, and explicit fallback states are unusually strong. |
| Reading experience | 3.5 | **Observed + expert review:** strong typography and concept anatomy, weakened on very long documents by distance and utility placement. |
| Navigation/findability | 3.0 | **Observed + expert review; needs moderated validation:** major destinations are visible, but global, Studio, and local navigation compete and terminology overlaps. |
| Task efficiency | 2.5 | **Observed risk + expert review; not user-timed:** high candidate counts and repeated evidence blocks indicate scan and keyboard-cost risk. |
| Graph usability | 3.0 | **Observed + expert review; needs moderated/AT validation:** powerful lenses and evidence panel, but dense defaults, onboarding, and canvas/non-canvas parity need refinement. |
| Accessibility | Not scored | **Observed + expert review:** good semantic intent, reflow, skip link, labels, and focus styling; current evidence is insufficient for a conformance-like score. |
| Responsive desktop/reflow | 4.0 | **Observed narrow-layout proxy + canonical contract evidence:** no horizontal overflow in audited scenarios and layout adapts cleanly; actual fresh browser zoom remains untested. |
| Visual system | 4.0 | **Observed Technical Light + expert review; broader themes contract-backed:** purposeful serif/sans/mono roles, semantic type colors, and restrained tokens. |
| Novice learnability | 2.5 | **Expert review only; needs novice testing:** product vocabulary and control density appear to assume knowledge of Context Packs, lenses, focus, evidence, and Studio concepts. |

## What already works well

### 1. The concept page expresses the product vision

The concept header, type treatment, description, entities, context action, structured document details, readable body, relationship cards, and previous/next navigation form a coherent editorial object. The demo `Orders` page is the clearest proof that OKF metadata can become useful interface rather than a frontmatter dump.

**Keep:** the type-aware header, article measure, structured table/code styling, typed relationship cards, and separation between prose and metadata.

### 2. Search is evidence-first, not confidence-theater

Search shows matched text, source coordinates, result type, path, excerpt controls, and “Why this result?” information. Copying a passage or context is a concrete workflow, not a generic AI affordance. This is a meaningful differentiator.

**Keep:** exact citations, explicit ranking mode, source exposure, deterministic Context Pack vocabulary, and fail-closed behavior.

### 3. The graph answers questions rather than only drawing edges

Map, Themes, Flow, Bridges, Recent, and Focus frame the graph around user questions. The right-side analytical panel, ranked themes, relation summaries, and non-canvas concept/relationship rows make the graph more useful than a conventional hairball.

**Keep:** question-led lenses, evidence side panel, typed relationship semantics, and explicit graph readiness.

### 4. The visual language is coherent

Serif headings signal reading and editorial authority; sans-serif UI controls remain functional; mono is reserved for identifiers, paths, and code. Type colors and icons recur across cards, concepts, and graph nodes. The interface is restrained and avoids generic dashboard decoration.

**Keep:** token discipline, quiet surfaces, semantic type accents, meaningful density, and Coffee Light as the strongest brand-forward theme family.

### 5. Responsive behavior is technically robust

None of the eleven audited scenarios introduced page-level horizontal overflow. At 1280 and the 720px 200%-equivalent viewport, content reflows without clipped code, tables, headers, or side panels. This is a strong baseline.

## Prioritized findings

Severity definitions:

- **P0:** core task is impossible, destructive, or unsafe.
- **P1:** material barrier for a major journey or accessibility mode; address before calling the desktop experience polished.
- **P2:** recurring efficiency, comprehension, or consistency problem.
- **P3:** local polish or low-frequency improvement.

### P1 — Fix first

#### F01 — The header lacks a single, conventional navigation hierarchy

**Evidence:** Observed on index, concept, Search, and graph. The top area mixes bundle identity and destinations with Appearance; a second row mixes agent state, Watching, comments, changes, activity, Commands, and Live. Concept pages add Rendered/Source controls into the same visual zone.

**Impact/severity basis:** Expert review; needs moderated task validation.

**Why it matters:** Users may need to repeatedly classify each item as destination, current mode, session state, or action. Graph and index use slightly different chrome arrangements. The correct control is visible but not always predictably placed.

**Recommendation:**

- Create a stable primary bar: bundle identity, Search, `Index`, `Graph`, then one `Studio` control and Appearance.
- Make the current destination unmistakable with `aria-current="page"` and a persistent visual state.
- Put `Rendered / Source / Split` in a page-local view bar below the primary navigation.
- Subject to maintainer approval, put Watching, Comments, Changes, Agent activity, Live status, and Commands inside an attention-aware Studio rail/drawer, with a compact status summary in the primary bar. This intentionally changes the current top-bar contract and must not weaken the directable-agent workflow.
- Use the same shell on index, concept, Search, and graph.

**Likely owners:** `render.py`, shared page templates, `wiki.css`, `studio.css`, `studio.js`, graph template/CSS.

**Verification:** A first-time user can identify current location and reach Index, Search, Graph, comments, and source view without trial activation; destination order remains identical across routes; keyboard focus order follows the visual hierarchy.

#### F02 — The docs index contains competing inventories and becomes a 6,598px browse wall

**Evidence:** Observed in `docs-index-1440.png`; 100 visible focusability candidates. The designed hero and typed card inventory are followed by another hand-authored “Demo / Four kinds of documentation / Outside the bundle” directory. Several concepts appear multiple times in different representations.

**Impact/severity basis:** Expert review; needs moderated browse-task validation.

**Why it matters:** The dashboard answers “what exists?” repeatedly instead of helping users choose where to start. The lower prose inventory makes the designed dashboard look like a preface to a second index.

**Recommendation:**

- Make the generated card inventory the canonical **browse representation**, while preserving hand-authored Markdown as the source of truth and retaining marker-safe behavior.
- Convert hand-authored index prose into a concise “Start here” section with three to five role-based entry points, subject to maintainer approval.
- Move full Diátaxis explanation to its own concept and link once.
- Add a clear default sort (recommended or recently changed) and optional type filter.
- Show one compact card per concept; do not repeat the complete inventory below.

**Likely owners:** bundle-root `index.md`, index composition in `render.py`, `index_page.html`, `wiki.css`.

**Verification:** The docs index remains under approximately two desktop viewport heights before optional full browsing; every concept has one primary card; five participants can locate install, live Studio, current spec, search, and graph entry points without scrolling through a duplicate directory.

#### F03 — Very long concept pages impose extreme reading and keyboard distance

**Evidence:** The current spec page is 41,967px high with 260 visible focusability candidates. The article rail, numerous inline links, source/context actions, relationship cards, and footer utilities are all in one linear document.

**Impact/severity basis:** Expert accessibility review; complete keyboard and assistive-technology traversal remains required.

**Why it matters:** The page shows semantic intent and clean reflow, but accessibility and efficient keyboard/assistive-technology traversal were not established in the initial capture. The scale indicates a material traversal risk; it does not prove that all 260 candidates are avoidable or sequential Tab stops.

**Recommendation:**

- Keep article links in the reading order, but remove nonessential card/container elements from repetitive tab paths.
- Provide a persistent “On this page” control with current-section indication, filterable section list, and `Home`/`End` support.
- Add a compact sticky page utility bar for `Source`, `Copy context`, and comments instead of repeating utility controls.
- Collapse secondary metadata and long connection inventories by default on documents over a defined threshold.
- Add a “Back to top / document details” return path at the end.

**Likely owners:** concept template, `wiki.css`, `wiki.js`, `studio.js`.

**Verification:** Keyboard journey from page entry to article start is short; users can jump to any heading and return to the outline; repeated relationship cards do not multiply unnecessary focus stops; screen-reader landmark/heading navigation remains coherent.

#### F05 — The index graph thumbnail has an unusable accessible name

**Evidence:** The DOM-derived candidate name begins with a long sequence such as `2 2 2 3 ... F Annotations…`, derived from SVG/graph text rather than the link purpose. A browser accessibility-tree or screen-reader confirmation remains required.

**Impact/severity basis:** Direct DOM defect evidence plus expert accessibility triage.

**Why it matters:** If the candidate matches the computed accessible name, screen-reader and voice-control users receive a long, meaningless name for a simple “open graph” link.

**Recommendation:** Give the link a concise explicit accessible name such as `Open knowledge graph — 35 concepts, 121 relationships`; mark decorative SVG internals `aria-hidden="true"` and `focusable="false"`; provide visible supporting text for the same summary.

**Likely owners:** index template/renderer and graph-export SVG generation.

**Verification:** Accessibility tree exposes one concise graph-preview link; no internal node label or numeric sequence contributes to its name.

#### F06 — The graph exposes 251 visible focusability candidates in the dense docs fixture

**Evidence:** Docs graph contains 251 visible focusability candidates; demo graph 76. The capture script exercised only the first 18 Tab moves, so it does not prove a 251-stop sequential order. The canvas is accompanied by many concept and relationship rows/buttons, which is valuable semantically but may be expensive if exposed as a flat tab sequence.

**Impact/severity basis:** Expert accessibility review; complete keyboard and screen-reader traversal remains required.

**Why it matters:** Canvas and alternative-view semantics should form one manageable composite experience. The current candidate count is a strong risk signal, not proof that non-pointer users cannot efficiently navigate it.

**Recommendation:**

- Treat lenses as a radiogroup/tablist with roving focus.
- Treat concepts and relationships as structured lists or a tree/grid with one Tab stop per collection and arrow-key movement inside.
- Add `Skip graph visualization` and `Jump to graph results/details` links.
- Ensure selection is synchronized between canvas, list, and detail panel.
- Announce selected concept, active lens, result count, and path state in one polite live region.

**Likely owners:** `graph_page.html`, `graph.js`, `graph.css`.

**Verification:** The graph shell has a bounded top-level Tab sequence; all concepts remain reachable via list/search and arrow keys; selection and focus are not trapped on the canvas; screen-reader announcements are concise.

#### F07 — Dense graph defaults reduce the “living atlas” to a debug-like overview

**Evidence:** `docs-graph-1440.png` shows label truncation, crossing edges, bundled-count badges, and a visually dense cluster. The right panel is strong, but the first visual read requires prior graph literacy.

**Impact/severity basis:** Expert visual review; needs moderated orientation testing.

**Why it matters:** The graph is technically information-rich but may not immediately answer “where should I start?” Dense bundles appear to penalize the primary Map lens most.

**Recommendation:**

- Default dense graphs to progressive labels: show labels only for top-ranked hubs, selection, hover/focus, and search matches.
- Increase separation and reserve a visible orphan/unlinked region.
- De-emphasize unselected edges; promote direction/labels only on selection or Flow lens.
- Add a ranked “Suggested starting points” list above themes.
- Test default fit and label policy at 25, 50, 100, and 250 concepts.

**Likely owners:** `graph.js`, `graph.css`, graph payload ranking.

**Verification:** At default 1440 view, top hubs and theme structure are readable without zoom; no important label collisions; selection reveals full local detail without losing global orientation.

#### F08 — The first-run graph tour blocks the workspace before orientation

**Evidence:** In fresh graph screenshots, a `Choose a lens` coach mark occupies the upper-right workspace immediately, overlapping the analytical region.

**Impact/severity basis:** Expert visual review; needs moderated first-run testing. Replacing the automatic tour is a proposed change to the approved design direction and requires maintainer approval.

**Why it matters:** The tour explains a control before the user has seen the graph unobstructed. `Back`, `Skip`, and `Next` add decisions during initial orientation.

**Recommendation:**

- Show an unobtrusive “Take a 45-second graph tour” invitation rather than auto-opening the tour.
- Start the tour only after explicit activation.
- Keep each step anchored without covering the target or detail panel.
- Remember dismissal per bundle/schema version and provide `Graph tour` in Help.

**Likely owners:** `graph.js`, graph template/CSS.

**Verification:** A first visit shows an unobscured usable graph; tour launch is discoverable; Escape closes and restores focus; reduced-motion behavior is respected.

### P2 — Improve next

Evidence and severity basis for the remaining findings:

| Finding | Evidence basis | Impact/severity basis |
|---|---|---|
| F09 | Observed Search screenshot, page height, and repeated result controls | Expert scan-efficiency review; needs timed comparison task |
| F10 | Observed labels and control structure | Expert learnability review; needs novice mode-prediction task |
| F11 | Observed actions plus Context Pack contract | Expert terminology/scope review; needs copy-outcome comprehension task |
| F12 | Observed/contract-backed terminology across reading and graph | Expert consistency review |
| F13 | Observed idle Studio row and zero counts | Expert hierarchy review; consolidation is a proposed contract change |
| F14 | Observed zero-count entry points; empty-panel behavior not fully exercised | Expert empty-state review; live non-empty/error proof required |
| F15 | Observed reflow screenshot and action labels | Expert action-safety/clarity review; mutation semantics contract-backed |
| F16 | Observed graph control layout | Expert convention and findability review; needs first-run tasks |
| F17 | Direct Cytoscape runtime warning on all fresh graph scenarios | Direct technical evidence plus expert interaction-risk triage |
| F18 | Observed card treatment and reflow ordering | Expert information-architecture review |
| F19 | Observed index content/order | Expert novice-orientation review; needs moderated validation |
| F20 | Observed semantic colors/icons plus canonical forced-colors contract evidence | Expert accessibility risk review; fresh dense-state proof required |
| F21 | Observed headings/labels across Search and graph | Expert content-design review |
| F22 | Observed icon/compact controls and DOM candidate labels | Expert accessibility/usability review; AT/tooltip proof required |
| F23 | Expert hypothesis only | Needs preference/task evidence; explicitly deferred behind disclosure fixes |
| F24 | Expert visual opinion based on canonical theme captures, not fresh all-theme scoring | Maintainer brand/product decision, not a defect |

#### F04 — Near-header reflow navigation works, but secondary utilities remain remote

**Evidence:** `demo-concept-200pct-equivalent.png` and canonical `zoom-400.png` show a collapsed `On this page` control directly after the concept header. Full Related content and Quick actions remain near the document end. The fresh 720×450 capture is a narrow-viewport proxy, not actual browser zoom.

**Impact/severity basis:** Expert review; the current near-header control prevents the original P1 claim. Keyboard activation, section selection, Escape, and focus restoration need actual 200%/400% testing.

**Recommendation:** Preserve the near-header `On this page` control. Validate its full keyboard behavior at actual zoom, keep Related as a separate disclosure, and move agent Quick actions into Studio rather than duplicating them at the document end.

**Verification:** At actual 200% and 400% zoom, activate `On this page`, choose a section, close via Escape, confirm focus restoration, and complete the journey without horizontal scrolling.

#### F09 — Search results expose too much evidence before the user asks for it

**Evidence:** `docs-search-1440.png` shows five results across 5,657px. Large excerpts, source passages, copy actions, context actions, and ranking explanations are repeated for every result.

**Why it matters:** Search is trustworthy but slow to scan. The answer list behaves like five open evidence dossiers.

**Recommendation:** Use a two-level result:

- Default row: rank, title, type, path, one highlighted excerpt, evidence summary, primary `Open` action.
- Expandable `Evidence and context`: source passage, reasons, citation details, and copy variants.
- Keep one consistent copy action in the row; place advanced context actions in the disclosure.
- Add sticky query/mode controls while scrolling long result sets.

**Verification:** Users can compare five results in one to two viewports; expanding a result preserves scroll position and has correct `aria-expanded` state.

#### F10 — Search control language is accurate but not novice-friendly

**Evidence:** `Best match`, `Exact words`, `Meaning`, `Tags`, `Entities`, `Relations`, “Prepare context,” and “Why this result?” are presented with little inline explanation.

**Recommendation:**

- Rename or subtitle modes in user language: `Best overall`, `Exact wording`, `Similar meaning`, `Tagged with`, `Mentions entity`, `Connected by relation`.
- Put expert backend names in help text, not the primary label.
- Add a one-line explanation of Context Packs near the first context action.
- Persist the selected mode in the URL and visibly summarize active filters.

**Verification:** Novice participants can predict which mode finds exact phrases versus related ideas and explain what “Copy context” will copy before activation.

#### F11 — Context actions are duplicated and their scope is hard to compare

**Evidence:** Search exposes result-level passage/context copy plus result-set preparation/copy. Concept pages expose context packs and exact-selection context. Similar verbs have materially different scope.

**Recommendation:** Establish one vocabulary:

- `Copy passage` = visible exact excerpt.
- `Copy concept context` = bounded context seeded by one concept.
- `Build context pack` = current query/result set.
- `Copy selection with citation` = selected text only.

Show the scope and estimated size before copying, and use one consistent toast/announcement pattern.

**Verification:** Every context action states source, scope, and outcome; clipboard success/failure is visible and announced; labels are consistent across Search and concept pages.

#### F12 — “Focus” is overloaded across page chrome, graph exploration, and selection state

**Evidence:** Existing contracts and UI use Focus for collapsing reading chrome and for graph-neighborhood exploration; graph instructions also discuss focused concepts.

**Recommendation:** Reserve `Focus view` for distraction-free reading and `Explore neighborhood` for graph topology. Use `Selected` for the current graph node. Update command palette aliases so expert shortcuts remain discoverable.

**Verification:** No visible control named only `Focus` can refer to two state models; help and command search use the same terms.

#### F13 — Studio status is prominent even when nothing requires attention

**Evidence:** `Agent idle`, `Watching`, `0 Comments`, `0 Changes`, `Agent activity`, `Commands`, and `Live` occupy a full row on every primary route.

**Recommendation:** Replace the idle row with one compact Studio status control, for example `Studio · Live · No pending work`. Expand it into task panels. Promote comments or changes only when counts are nonzero or attention is required.

**Verification:** Reading routes gain vertical calm; nonzero comments/changes remain immediately visible; status is not color-only; all previous functions remain available within one activation.

#### F14 — Activity and change states need clearer action-oriented empty states

**Evidence:** Zero-count buttons advertise features but do not explain when or why they matter.

**Recommendation:** Empty panels should explain the workflow in one sentence and offer the next relevant action: select text to comment, start Watching, or open the agent-loop instructions. Avoid rendering a complex panel with only “none.”

**Verification:** Each empty state names the feature, why it is empty, and one next action; panels restore focus correctly on close.

#### F15 — Quick actions read like generic expansion rows, not an agent-directed workflow

**Evidence:** At reflow width, `Add section`, `Split document`, `Add links`, and `Enrich content` appear as equal disclosure rows at the end of the page.

**Recommendation:** Group them under `Ask the agent to improve this concept`; give each action a one-line outcome and distinguish actions that open a prompt from immediate mutations. Move the group into the Studio rail rather than the reading rail.

**Verification:** A user can tell whether activation will preview, ask for instructions, or mutate; destructive or broad actions never appear immediate.

#### F16 — Graph control placement and terminology still assume expert graph knowledge

**Evidence:** `Type`, Reset view, bundled relationships, zoom controls, lens cards, ranked themes, selection instructions, and the canvas all compete in one viewport.

**Recommendation:**

- Combine query and filters into one `Find in graph` bar.
- Group zoom/fit/fullscreen as one conventional map-control cluster with tooltips.
- Keep `Reset view` near that cluster, not in the filter row.
- Keep lenses in one compact panel; move advanced relationship bundling and layout controls behind `Graph settings`.

**Verification:** First-time users can find a concept, change lens, fit the graph, and open a concept without reading instructional prose.

#### F17 — Custom wheel sensitivity creates inconsistent zoom behavior

**Evidence:** Cytoscape emits the warning on every audited graph route: custom wheel sensitivity can feel unnatural across mainstream mouse/OS combinations.

**Recommendation:** Remove the custom sensitivity unless hardware-specific evidence justifies it. Prefer the library default and expose explicit zoom controls for precision.

**Verification:** No Cytoscape warning in clean graph load; trackpad and wheel behavior are tested on at least macOS and Windows/Linux hardware profiles.

#### F18 — Page-local navigation disappears into the same card language as metadata and actions

**Evidence:** Related, Sections, and Quick actions use visually similar bordered containers. On desktop they are understandable but equal-weight; at reflow widths they form a long stack.

**Recommendation:** Give `On this page` a navigation-specific treatment and current-section state; render Related as content recommendations; move Quick actions to Studio. Distinguish each by placement and interaction, not only heading text.

**Verification:** Users can identify navigation versus content versus operations at a glance and by landmark/accessible name.

#### F19 — The index does not clearly prioritize novice entry points versus expert inventory

**Evidence:** The hero emphasizes Annotations v2, graph exploration, release notes, graph thumbnail, stats, type navigation, and the complete inventory before basic “how do I use this?” paths.

**Recommendation:** Add a role-based first-run strip: `Use from a clone`, `Open the live Studio`, `Find a command`, `Understand the format`. Personalize only through explicit, local, inspectable state; do not hide the complete inventory.

**Verification:** New users find setup and the live Studio faster; existing users can jump directly to inventory/search with no extra modal.

#### F20 — Dense theme/type color usage needs a non-color-first audit

**Evidence:** Type color and icon are generally paired, but graph nodes, tiny legends, tags, statuses, and relationship accents create many low-area semantic marks.

**Recommendation:** Confirm every semantic color is paired with text, icon, shape, dash, or position. Test forced-colors and all eight family/mode combinations on dense Search and graph states, not only representative pages.

**Verification:** Type, selection, relation direction, broken state, freshness, comment state, and active lens remain distinguishable in grayscale and forced-colors mode.

### P3 — Polish

#### F21 — Search and graph headings use inconsistent editorial voice

Index/concept surfaces use product/editorial language, while Search uses form-centric labels and graph sometimes uses analytical/debug phrasing. Adopt a voice rule: headings describe the user’s question; controls describe the action; metadata uses domain terminology.

#### F22 — Some controls depend on icon literacy

Agent activity and compact graph controls need persistent tooltips and robust accessible names. Avoid one-character or icon-only states where adjacent text can provide clarity.

#### F23 — Result and card density could use an explicit compact/comfortable preference

Experts may prefer the current density; novices may benefit from more whitespace. A user-selectable density setting belongs in Appearance only after F01–F20 are addressed; it should not compensate for poor disclosure.

#### F24 — Theme identity is strong but not equally brand-forward

Coffee Light best supports the “editorial atlas” identity. Technical is a strong default for engineering users but feels closer to documentation tooling. Consider Coffee as the showcase/marketing theme while preserving Technical as the conservative application default.

## Journey audit

| Journey | Current assessment | Main friction | Target outcome |
|---|---|---|---|
| Orient from index | Works, but over-complete | Duplicate inventories and too many equally prominent starts | One clear “start here,” one canonical inventory, graph/search always visible |
| Find a concept | Strong search and type browsing | Search evidence is expanded by default; mode language is expert-centric | Compare results quickly, disclose evidence on demand |
| Read a concept | Strongest surface | Very long documents and utility density | Calm article, persistent local navigation, utilities one action away |
| Copy evidence/context | Powerful and trustworthy | Similar verbs with different scope | Scope is explicit before activation and feedback is consistent |
| Leave an agent comment | Differentiated core workflow | Feature is not self-evident until text is selected; Studio chrome competes while idle | Selection reveals a clear action; comments become prominent only when active |
| Review changes/activity | Functionally present | Idle controls occupy permanent space; panel purposes overlap | One Studio control summarizes attention and opens task-specific panels |
| Explore graph | Analytically rich | Dense default, obstructive first-run tour, high keyboard cost | Unobstructed map, obvious starting points, efficient list/canvas parity |
| Change appearance | Mature state model | Competes with primary navigation | Conventional utility placement, clear checked state, reliable reset |
| Work at 200%-equivalent width | Reflows cleanly and exposes `On this page` near the header | Related and Quick actions remain remote; actual browser zoom is untested | Preserve near-header navigation and verify equivalent task access at real 200%/400% |
| Understand sharing | Secure contract exists | Local/public/edit authority is an operator concept, not clearly visible in reading UI | Compact status explains local/public and read/edit authority without exposing secrets |

## Recommended information architecture

### Primary application shell

1. **Bundle identity** — returns to Index.
2. **Search** — persistent field or button with `/` shortcut.
3. **Index** — primary destination.
4. **Graph** — primary destination.
5. **Studio** — compact attention/status summary; opens collaboration and operations.
6. **Appearance** — conventional utility at the end.

### Page-local controls

- Concept: `Rendered`, `Source`, `Split`; `On this page`; context actions.
- Search: query, mode, filters, result count, context-pack builder.
- Graph: find/filter, lens, view controls, selection/detail state.

### Studio panel

- Comments.
- Changes and undo.
- Agent activity.
- Watching/live connection.
- Commands/quick actions.
- Sharing/authority status.

This separation keeps capability intact while clarifying ownership: application shell chooses a place, page-local controls change the current place, and Studio controls coordinate work.

## Recommended interaction contracts

### Disclosure

- Default views show what is needed to choose the next action.
- Evidence, metadata, and advanced controls remain one explicit action away.
- Opening a disclosure must not change scroll unexpectedly.
- URL-addressable state is preferred for Search mode/filters and graph lens/selection when safe.

### Focus and keyboard

- One Tab stop enters a composite control; arrow keys move within it.
- Escape closes only the top-most owned overlay and restores its trigger.
- A page change moves focus to the new page heading unless initiated in a context where preserving focus is more useful and announced.
- Status updates use a concise polite live region; errors are assertive only when immediate correction is required.
- Canvas functionality has an equally capable structured alternative, not hundreds of flat Tab stops.

### Copy feedback

Every copy action should expose:

1. what will be copied;
2. the scope/size when it is more than visible text;
3. success or a recoverable manual-copy fallback;
4. a screen-reader announcement;
5. no unexplained replacement of the user’s current selection.

### Destructive and agent-directed actions

- Labels state whether the action opens a prompt, creates a proposal, or mutates immediately.
- Broad actions preview scope.
- Undo availability is visible before confirmation.
- Public/tunnel editing authority is plainly stated without exposing tokens or internal routes.

## Accessibility worklist

This section is a remediation plan, not a conformance claim.

| Area | Required change | Relevant WCAG 2.2 themes | Proof |
|---|---|---|---|
| Graph thumbnail | Concise link name; hide decorative SVG internals | 1.1.1, 2.4.4, 4.1.2 | Accessibility-tree assertion and voice-control smoke test |
| Graph collections | Composite list/tree/grid keyboard model; skip links | 2.1.1, 2.4.3, 2.4.7, 4.1.2 | Full keyboard journey plus screen-reader session |
| Long concept pages | Reachable local nav, bypass paths, reduced utility stops | 2.4.1, 2.4.3, 2.4.6 | Keyboard traversal budget and heading/landmark inspection |
| Reflow utilities | Preserve the existing near-header `On this page`; verify activation, Escape, restoration, and remote secondary actions at actual zoom | 1.4.10, 2.4.3 | Browser zoom checks at 200% and 400% |
| Overlays/panels | Correct role/name, focus entry, Escape, restoration | 2.1.2, 2.4.3, 4.1.2 | Matrix across Appearance, Studio, comments, graph tour |
| Status and copy feedback | Concise live-region announcements | 4.1.3 | Accessibility-tree/live-region assertions and screen-reader smoke |
| Color semantics | Non-color cues in graph/status/type marks | 1.4.1, 1.4.3, 1.4.11 | Grayscale, forced-colors, and contrast checks |
| Target size | Verify compact graph/header controls | 2.5.8 | Computed geometry assertion at desktop/reflow widths |
| Consistent placement | Stable shell and help placement | 3.2.3, 3.2.4, 3.2.6 | Cross-route DOM/order assertions |

## Phased action plan

Severity, release blocking, and implementation sequence are separate. P1 means the issue materially affects a major journey or accessibility mode; it does not mean every P1 can safely share one implementation bundle. The shell/focus ownership work must land before page-specific compression or graph tuning.

| P1 | Release-blocking for “polished desktop” | Sequence | Dependency rationale |
|---|---|---|---|
| F01 shell hierarchy | Yes, after maintainer approves the contract change | Phase 1A | Establishes shared navigation and focus ownership used by every later surface |
| F02 duplicate index | Yes | Phase 2 | Requires the shell and an approved marker-safe content decision; can begin content analysis in parallel |
| F03 long-document traversal | Yes | Phase 1B | Depends on page-local control placement from F01 |
| F05 graph-preview candidate name | Yes | Phase 1A | Independent, low-risk accessibility fix |
| F06 graph candidate traversal | Yes | Phase 1B/3 | Skip/composite entry can land early; full synchronized graph model follows graph-state work |
| F07 dense graph legibility | Yes | Phase 3 | Requires size-threshold fixtures and should not be mixed with shell ownership changes |
| F08 automatic graph tour | Approval required; block only if maintainers accept opt-in direction | Phase 3 | Changes the approved first-visit contract and belongs with graph onboarding |

“Release-blocking” here applies to closure of this proposed UX program, not to the already-published technical release record.

### Phase 0 — Baseline and decision record (small)

**Goal:** prevent local fixes from creating a second navigation or terminology system.

1. Record the application-shell IA and vocabulary decisions from this audit.
2. Add measurement hooks for journey completion, disclosure use, and keyboard traversal without collecting document content.
3. Capture baseline timings with five representative tasks and at least three novice and three expert users if available.
4. Confirm the selected accessibility test stack and screen-reader/browser combinations.

**Exit criteria:** approved shell diagram, vocabulary table, baseline task script, and accessibility matrix.

### Phase 1 — Navigation and critical accessibility (medium, highest priority)

**Goal:** make every major route predictable and efficiently operable.

1. Implement the stable primary shell from F01.
2. Move idle Studio controls into one attention-aware Studio panel.
3. Move Rendered/Source/Split into page-local controls.
4. Fix graph-thumbnail naming.
5. Add graph skip links and composite keyboard navigation for concepts/relationships.
6. Preserve and verify the existing near-header `On this page` navigation at actual 200%/400% zoom.
7. Remove or justify custom Cytoscape wheel sensitivity.

**Primary files:** shared render/page shell, `wiki.css`, `studio.css`, `studio.js`, graph template/CSS/JS.

**Exit criteria:**

- Same destination order on every route.
- Current destination exposed visually and through `aria-current`.
- No meaningless graph-preview accessible name.
- Graph top-level Tab sequence is bounded.
- Concept section navigation is reachable near the header at 200% and 400%.
- Overlay focus matrix passes.

### Phase 2 — Index and Search disclosure (medium)

**Goal:** reduce time-to-choice without weakening evidence.

1. Replace duplicate index inventories with one canonical card set and a concise Start here section.
2. Add role-based entry points and explicit default ordering.
3. Redesign Search results as compact summaries with expandable evidence.
4. Normalize search-mode and context-action language.
5. Keep query controls available while scanning long results.

**Primary files:** bundle `index.md`, index/search templates, `render.py`, `wiki.css`, retrieval UI JavaScript.

**Exit criteria:**

- Docs index no longer repeats the full inventory.
- Five Search results can be compared in at most two 1440×900 viewports in the default state.
- All exact evidence remains available without navigation.
- Search state survives reload/back through the URL.

### Phase 3 — Graph legibility and onboarding (large)

**Goal:** preserve analytical power while making the default map inviting.

1. Replace auto-open tour with an explicit invitation.
2. Add progressive label policy and dense-graph layout thresholds.
3. Add Suggested starting points before the theme inventory.
4. Reorganize find/filter, lenses, view controls, and advanced graph settings.
5. Synchronize canvas/list/detail selection and announcements.
6. Validate default behavior at several graph sizes and data qualities.

**Primary files:** `graph.js`, `graph.css`, graph template, graph payload/ranking seam.

**Exit criteria:**

- Unobstructed first visit.
- Major hubs/themes readable at default fit.
- No important label collisions in acceptance fixtures.
- Pointer, keyboard, and screen-reader users can select, inspect, follow a relation, and open a concept.

### Phase 4 — Studio workflow clarity and polish (medium)

**Goal:** make agent collaboration understandable without permanent console density.

1. Add explanatory empty states for Comments, Changes, and Activity.
2. Reframe Quick actions as agent-directed proposals with clear scope.
3. Explain local/public and read/edit authority in the Studio status surface.
4. Normalize copy feedback and Context Pack scope.
5. Review visual density, tooltips, themes, forced colors, reduced motion, and target size.

**Exit criteria:** users can predict the outcome of every Studio action; zero-state chrome is calm; public authority is understandable; copy and error feedback is accessible.

## Implementation sequencing and ownership

### Parallel-safe slices

- Graph-preview accessible name and SVG semantics.
- Search result disclosure design and copy vocabulary.
- Index content consolidation.
- Cytoscape wheel-sensitivity correction.
- Empty-state copy and Studio information architecture.

### Single-writer/hot surfaces

- Shared page shell and top navigation.
- `studio.js` overlay/focus ownership.
- `graph.js` selection, keyboard, and lens state.
- Shared token blocks in `wiki.css`/`studio.css`/`graph.css`.

Implement the shell and terminology decisions before page-specific visual polish. Do not independently redesign headers on index, Search, concept, and graph.

## Validation plan

### Automated

- Cross-route shell order, active destination, labels, and `aria-current`.
- Accessibility-tree assertions for graph preview and all icon-only controls.
- Keyboard matrix for primary nav, page-local modes, Appearance, Studio, comments, graph tour, graph list/canvas, and reflow navigation.
- Search disclosure state, URL persistence, copy feedback, and no-result/error states.
- 200% and 400% zoom with no horizontal overflow and equivalent task access.
- Forced-colors, reduced-motion, and all theme-family/mode smoke checks.
- Graph fixtures at small, medium, dense, broken-link, orphan-heavy, and no-relation states.

### Browser/live

For each changed route, capture fresh screenshots and execute:

1. novice orientation from index;
2. exact and meaning-based Search;
3. open result, jump to section, copy cited context;
4. select text and open/cancel comment composer;
5. inspect Comments/Changes/Activity empty and non-empty fixtures;
6. switch Rendered/Source/Split and restore state;
7. graph find, lens, select, relation/path, reset, and open concept;
8. change/reset Appearance;
9. repeat key paths at 1280, 200%, keyboard-only, and screen-reader.

### Moderated usability

Use task success, time, wrong turns, help requests, and confidence—not preference ratings alone.

Suggested tasks:

- “You just cloned this repo. Find the shortest path to starting the live Studio.”
- “Find the current rule for public tunnel editing and copy the cited evidence.”
- “Show which concepts connect to HTTP server routes and open the most relevant one.”
- “Leave an instruction on one exact sentence, then find where the agent would see it.”
- “Switch to source view, then return to a distraction-free rendered view.”

Target outcomes after Phase 3:

- 90% first-attempt success for primary navigation and Search tasks.
- Median time under 30 seconds to locate Index, Search, Graph, comments, and source view.
- No more than one wrong activation on graph-lens and context-copy tasks.
- Keyboard users complete equivalent tasks without traversing every concept or relationship.

## Acceptance gates

Do not close the action plan based only on screenshots or unit tests.

- **Functional:** core journeys work with real server data and preserve state appropriately.
- **Accessibility:** keyboard and screen-reader sessions pass the defined matrix; unresolved critical/serious issues block closure.
- **Visual:** fresh screenshots reviewed at 1440, 1280, 200%, dark, forced colors, and dense-content fixtures.
- **Evidence:** every P1 finding has a direct before/after assertion or moderated result.
- **Parity:** live, static, SPA, and single-file outputs retain honest capability and fallback behavior.
- **Security:** navigation and Studio changes do not widen mutation authority or expose session/tunnel secrets.

## Decisions requested from maintainers

1. Approve the primary-shell hierarchy: destinations in the shell, page modes local, Studio operations in one rail.
2. Approve progressive disclosure as the default for Search evidence, graph controls, and long-page metadata.
3. Choose whether Coffee is the showcase theme while Technical remains the application default.
4. Approve the terminology split: `Focus view` for reading, `Explore neighborhood` for graph topology.
5. Confirm whether the docs bundle’s hand-authored root index may be reduced to Start here content so the generated inventory is canonical.

## Recommended first implementation bundle

The first bundle should include only changes that establish the system and remove material barriers:

1. shared navigation hierarchy and active-route semantics;
2. compact Studio status/rail without removing functionality;
3. graph-preview accessible name;
4. graph skip links and composite keyboard entry points;
5. reflow-safe `On this page` navigation;
6. default Cytoscape wheel behavior;
7. focused browser/accessibility proof for those changed contracts.

Defer Search/index visual compression and graph layout tuning until this shell is stable. That order prevents later work from being built on inconsistent navigation and focus ownership.

## Evidence index

- Capture evidence (not in the public tree; removed before publication and kept only in Git history): the `ui-ux-audit-2026-08-11` manifest (audited revisions, browser/profile notes, file inventory, SHA-256 hashes), its capture script, the DOM/focus/landmark data, and the screenshots cited above (`docs-index-1440.png`, `docs-concept-1440.png`, `docs-search-1440.png`, `docs-graph-1440.png`, `demo-concept-1280.png`, `demo-concept-200pct-equivalent.png`, `demo-graph-1440.png`), plus the historical `ui-overhaul-final` canonical scenarios and manifests for `348e66f`, which are not proof for the current candidate.
- `docs/design/ui-overhaul-plan.md` — original “living atlas” direction and historical problems.
- `docs/design/ui-overhaul-release-matrices.md` — state, parity, focus, capture, and lifecycle contracts.
- `README.md` — product positioning and user workflows.
- `SKILL.md` — operator behavior and collaboration loop.
- `docs-bundle/reference/spec.md` — binding current behavior.

## Bottom line

The v2 desktop product is already differentiated and technically mature. The right next move is not more surface area and not a new visual language. It is to make the existing system feel lighter by clarifying ownership:

- destinations belong to navigation;
- view modes belong to the page;
- collaboration belongs to Studio;
- evidence appears progressively;
- graph data remains fully reachable through a bounded, verified keyboard and assistive-technology model rather than an untested high-candidate focus surface.

That refinement would preserve the product’s strongest qualities—plain-file trust, evidence-rich retrieval, graph semantics, and directable agent collaboration—while making them substantially easier to discover and use.
