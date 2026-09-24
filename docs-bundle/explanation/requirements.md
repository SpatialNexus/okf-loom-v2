---
type: Explanation
title: Original Requirements
description: The original user requirements brief that shaped okf-loom, with a traceability matrix mapping each requirement to the docs and code that implemented it.
resource: /explanation/requirements.md
tags: [requirements, history, traceability]
timestamp: "2026-06-29T00:00:00Z"
---

# Requirements

This document captures the original user requirements for okf-loom
**verbatim** and traces each requirement to the artifact that delivers
it. It exists so future maintainers (humans or agents) can verify the
repository still meets its brief, and so scope changes are explicit.
The current distribution model is the cloneable repository as a repo-local
skill layout. Direct checkout execution with `scripts/okf-loom` is the
primary runtime path.

## Original prompt (verbatim, 2026-06-27)

> look at the new okf format stuff
> https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf
>
> i want to build something into my meta harness so when asked to do anything related it uses this system for everything its good at. also when a user asks to improve or otherwise things this would be good for it will ask to transition things to this format
>
> we need
> a note for the agent so they know it exists
> skills the agent can load to understand what it is/how to use it - prob need a basic and advanced or something so we dont need to load lots for just quick working with the files but if we want to do more complex stuff its got the depth
>
> we need some scripts that given a tree will validate/verify everything made/links/structure/content etc
>
> scripts that when run stands up a viewable endpoint that is a wiki style thing with fully navigable and node connected everything views
>
> scripts that do all the common functionality like searching by term, relation, entity, semantics, etc etc
>
> scripts that when the agent runs them will work out what things like all the above is missing from various things and get the agent to call the script with the correct links or semanitcs or relations or entities or etc etc to do the updates and indexing
>
> easy ways to have a defined place to link into or override the viewer stuff
>
> we need this all properly researched. thought through, developed and tested such that we have a full and complete package we can embed into any agent/meta harness setup (dont need things specific to any, that will be done later - this is purely fully generic)
>
> we need to research what other people are doing and bring in the best parts of all those things too into this system
>
> we need to ensure that everything is done in a fully forward compat way thinking about ensuring its easy and sane to extend/improve/itterate on this and all the structure of whats setup/made in the users repo can be easily/automatically updated going forward

## Requirements traceability matrix

| # | Requirement | Delivered by |
|---|---|---|
| 1 | Build something into meta-harness that uses OKF for everything it's good at | [`SKILL.md`](../../SKILL.md) (loadable skill entrypoint + resource map) + [`resources/overview.md`](../../resources/overview.md) (trigger list) + [`docs-bundle/reference/embedding_guide.md`](/reference/embedding_guide.md) (integration shapes + harness-specific recipes) |
| 2 | When user asks to improve things OKF suits, agent asks to transition to OKF | [`resources/overview.md`](../../resources/overview.md) §"Default agent behavior" plus [`SKILL.md`](../../SKILL.md) routing |
| 3 | A note so the agent knows OKF exists | [`SKILL.md`](../../SKILL.md) at repository root; [`AGENTS.md`](../../AGENTS.md) is a thin pointer for AGENTS-first harnesses |
| 4 | Skill resources (progressive loading from one repo skill) | [`resources/{format-basics,authoring,advanced-operations}.md`](../../resources/) — focused Markdown resources referenced from the root `SKILL.md` |
| 5 | Scripts that validate/verify a tree (links/structure/content) | [`scripts/okf_loom/validate.py`](../../scripts/okf_loom/validate.py) — upstream OKF 0.2 §11 conformance plus soft checks, including optional metadata families. CLI: `scripts/okf-loom validate <bundle> [--strict] [--checks …] [--format json]` |
| 6 | Scripts that stand up a viewable wiki endpoint (navigable, node-connected) | [`scripts/okf_loom/server.py`](../../scripts/okf_loom/server.py) (the canonical GET/POST route inventory is maintained in [HTTP routes](/reference/http_routes.md); stdlib-only, hot-reload) + [`scripts/okf_loom/render.py`](../../scripts/okf_loom/render.py) (`single-file` / `static` / `spa` build targets) + [`scripts/okf_loom/viewer/`](../../scripts/okf_loom/viewer/) (templates/static assets, Cytoscape.js + stdlib markdown renderer). CLI: `scripts/okf-loom serve` / `scripts/okf-loom render` / `scripts/okf-loom build` |
| 7 | Scripts for common functionality (search by term/relation/entity/semantics) | [`scripts/okf_loom/search.py`](../../scripts/okf_loom/search.py) — zero-dep field-weighted BM25 (`LexicalBackend`) + **SemanticLiteBackend** (trigram+token TF cosine, zero-dep) + **HybridBackend** (RRF fusion, zero-dep) + **ENTITY**/**RELATION** search modes. All dependency-free. CLI: `scripts/okf-loom search <bundle> <query> [--mode lexical\|semantic\|hybrid\|tag\|entity\|relation]` |
| 8 | Scripts that find what's missing and drive the agent to fix it | [`scripts/okf_loom/discover.py`](../../scripts/okf_loom/discover.py) (6 rules) + [`scripts/okf_loom/plan.py`](../../scripts/okf_loom/plan.py) (action-envelope planner: `PlannedAction`/`Plan`, discovery + index staleness + relation mirroring → `scripts/okf-loom plan`) + [`scripts/okf_loom/update.py`](../../scripts/okf_loom/update.py) (8 idempotent op kinds incl. `add_entity`) + [`scripts/okf_loom/cli.py`](../../scripts/okf_loom/cli.py) (`scripts/okf-loom write-concept`, `link-add`, `entity-add`, `set-frontmatter`, `repair`, `init`) |
| 9 | Easy defined place to link into / override viewer | Five extension points (see [`scripts/okf_loom/viewer/OVERRIDES.md`](../../scripts/okf_loom/viewer/OVERRIDES.md)): `<bundle>/.okf-loom/viewer/templates/*.html`, `<bundle>/.okf-loom/viewer/static/*`, `<bundle>/.okf-loom/viewer/palette.json`, `<bundle>/.okf-loom/viewer/config.json`, and the `ViewerPlugin` Protocol |
| 10 | Properly researched, thought through, developed, tested | [`docs-bundle/explanation/research.md`](/explanation/research.md) (5,755 words across 15 systems + Synthesis sections) + [`docs-bundle/reference/architecture.md`](/reference/architecture.md) (module map + design rationale + extension points + forward-compat mechanisms) + the pytest suite (browser-proof tests skip cleanly when optional dependencies are absent), including 23 `@pytest.mark.integration` tests against the upstream `ga4` / `stackoverflow` / `crypto_bitcoin` bundles |
| 11 | Full and complete repository/toolkit, embeddable in any agent/meta-harness | No primary runtime install step for normal docs validation; PyYAML is preferred when present and a conservative built-in YAML fallback ships with the skill; pure-stdlib viewer/server; CLI works standalone; library is importable; HTTP server is embeddable in a daemon thread; harness-by-harness recipes in [`docs-bundle/reference/embedding_guide.md`](/reference/embedding_guide.md) |
| 12 | Generic only — NOT harness-specific | No agent-framework code ships in the repository. All harness-specific wiring lives in [`docs-bundle/reference/embedding_guide.md`](/reference/embedding_guide.md) as recipes the harness owner applies externally. okf-loom contains no imports of opencode / LangChain / Claude / Codex SDKs. |
| 13 | Research what others are doing, bring in the best parts | [`docs-bundle/explanation/research.md`](/explanation/research.md) covers: **Obsidian** (graph view, backlinks, Dataview, plugin architecture), **Logseq** (outliner + block refs), **Notion** (databases, relations, rollups), **MkDocs Material** (nav, search, tags plugin, plugins API), **Hugo/Jekyll** (taxonomies, shortcodes), **Docusaurus** (versioning, presets, theme swizzling), **Quartz v4** (DEEP — transformer/filter/emitter pipeline, ContentIndex, CrawlLinks, FolderPage/TagPage, popovers — primary architectural influence), **Foam/Dendron** (hierarchies, schemas, multi-vault), **semantic search** (sentence-transformers, sqlite-vec, FAISS, hybrid fusion, cross-encoders), **NER/relation extraction** (spaCy, GLiNER, REBEL, LLM-based), **knowledge graphs** (Neo4j, RDF/SPARQL, JSON-LD, schema.org), **Diátaxis** documentation taxonomy, **DEVONthink/Roam/Bear** (consumer expectations), **search libraries** (MiniSearch, Pagefind, lunr.js, FlexSearch), **self-contained HTML patterns** (Cytoscape + marked vs SPA vs server-rendered tradeoffs) |
| 14 | Forward-compat: easy/sane to extend/improve/iterate | [`scripts/okf_loom/extensions.py`](../../scripts/okf_loom/extensions.py) — capability registry across core / recommended / optional tiers; `okf_extensions:` and governed keys activate extensions. Pluggable `SearchBackend` and consent-gated `ViewerPlugin` protocols remain separate. `upgrade` previews/checks without writes; explicit apply supports conservative 0.1 → 0.2 migration. See [Forward compatibility](/reference/architecture.md#forward-compatibility). |
| 15 | Auto-updatable structure in user's repo | [`scripts/okf_loom/index.py`](../../scripts/okf_loom/index.py) regenerates upstream §8 listings inside paired markers or unmarked root indexes with literal `generated: true`; concept generation mappings never own regeneration. `--frozen` refuses authored selections, not managed writes. [`scripts/okf_loom/log.py`](../../scripts/okf_loom/log.py) preserves append-only upstream §9 history. Mutators preserve unknown frontmatter and authored content; discovery plans require explicit application. See [current spec](/reference/spec.md). |
| 16 | Wikilink support (`[[…]]` body syntax; current spec §4) | [`scripts/okf_loom/parse.py`](../../scripts/okf_loom/parse.py) (wikilink extraction: `[[target]]` and `[[target\|Label]]`, resolved to concept ids via the same resolver as markdown links) + [`scripts/okf_loom/model.py`](../../scripts/okf_loom/model.py) (`bundle.has_wikilinks` scans bodies; `okf.cap.wikilinks` auto-activates and feeds `capabilities()`; wikilinks emit graph edges with `form="wikilink"`) + [`scripts/okf_loom/validate.py`](../../scripts/okf_loom/validate.py) (INFO finding `link.wikilink_present` suggesting conversion to standard markdown links) |
| 17 | Live collaborative studio — user directs via comments, agent authors the bundle live, no full-page refresh (current spec §10-§12) | [`scripts/okf_loom/studio.py`](../../scripts/okf_loom/studio.py) (`Studio`, `EventBus`, comment lifecycle, presence, undo + group-undo) + [`scripts/okf_loom/server.py`](../../scripts/okf_loom/server.py) (SSE `/__events`, `POST /__comment`/`/__apply`/`/__undo`/`/__presence`, per-doc `/__data/doc`, `_BundleWatcher` change-diff) + `scripts/okf_loom/viewer/static/{live.js,studio.js,studio.css}` (in-place patching, view modes, comment UI). CLI: `scripts/okf-loom serve <bundle>` is the full studio by default. Posture: the agent edits freely via the mutators — no review gate (current spec §10) |
| 18 | Headless change feed for the agent loop (current spec §12) | [`scripts/okf_loom/watch.py`](../../scripts/okf_loom/watch.py) (`run_watch(...)`, reuses `_BundleWatcher`; appends the durable `.okf-loom/session/events.jsonl` shared with `serve`). CLI: `scripts/okf-loom watch <bundle> [--emit {jsonl,text}] [--since <id>] [--auto-repair] [--debounce-ms N]` |
| 19 | Scoped, incremental discovery & planning (current spec §7) | [`scripts/okf_loom/discover.py`](../../scripts/okf_loom/discover.py) (`discover_suggestions(..., scope=…)`) + [`scripts/okf_loom/plan.py`](../../scripts/okf_loom/plan.py) (`build_plan(..., scope=…)`) + [`scripts/okf_loom/cli.py`](../../scripts/okf_loom/cli.py) (`--scope ID,ID` restricts rules; `--neighbors` expands to 1-hop graph neighbors). CLI: `scripts/okf-loom discover\|plan <bundle> --scope ID,ID [--neighbors]` |
| 20 | `scripts/okf-loom serve` operational flags — read-only kiosk, network bind, no-SSE (current spec §9-§14) | [`scripts/okf_loom/cli.py`](../../scripts/okf_loom/cli.py) (`cmd_serve`) + [`scripts/okf_loom/server.py`](../../scripts/okf_loom/server.py) (`run_server(..., studio_edit, studio_live)`). Flags: `--no-edit` (read-only kiosk — live reads on, all `POST` endpoints disabled), `--public` (network bind `0.0.0.0` + WARNING; default `127.0.0.1`), `--no-watch-ui` (disable SSE) |
| 21 | Trust model — single-user local admin; invisible CSRF + origin/host guard (current spec §14) | [`scripts/okf_loom/server.py`](../../scripts/okf_loom/server.py) (per-session `X-OKF-Token` embedded in the served page; `Origin`/`Host ∈ allowed_hosts` check on all mutating endpoints; loopback-by-default bind) + [`scripts/okf_loom/studio.py`](../../scripts/okf_loom/studio.py) (token issuance). The user is never gated; this guards the port and the bundle's code, not the person or their agent |
| 22 | `studio:` config section in `okf-loom.config.yaml` (current spec §5 and §10) | [`scripts/okf_loom/config.py`](../../scripts/okf_loom/config.py) parses `StudioConfig`. The serve runtime consumes `edit`, `live`, `session_dir`, `log_edits`, `max_sse_clients`, `allowed_hosts`, `theme`, `events_max_bytes`, and `events_keep`; `enrich_offer`, `auto_enrich`, and `debounce_ms` are currently reserved metadata rather than server behavior. Constraints have [limited Studio write-funnel coverage](/reference/config_yaml.md#constraints), not general policy enforcement. A plain `scripts/okf-loom serve` still enables the implemented studio surfaces; CLI flags override applicable config. |

## Constraints

### User-stated

- **"dont need things specific to any [harness], that will be done later - this is purely fully generic"** — No harness-specific code in the repository. ✅ Verified: no imports of opencode / LangChain / Claude / Codex SDKs anywhere in `scripts/`.
- **Forward-compat and auto-updatable are hard requirements, not nice-to-haves.**

### Maintained constraints

- Match upstream OKF 0.2 with explicit legacy compatibility — don't invent a parallel format.
- Diagnose upstream bugs we discover and document current behavior in the canonical spec.
- Zero-dependency by default for portability into air-gapped or constrained harnesses.
- Idempotent mutations so re-running discovery→update loops is safe.

## Out of scope

### Explicitly deferred per user

- **Harness-specific wiring** (opencode agents, Claude Code skill configs, MCP server, etc.). The recipes live in `docs-bundle/reference/embedding_guide.md`; no harness-specific code ships in okf-loom.

### Promoted into the current okf-loom spec

These are optional capabilities in okf-loom 2.0.0 over the OKF 0.2 wire format:

- **Lexical / hybrid / semantic-lite search** — `LexicalBackend` (BM25),
  `SemanticLiteBackend` (zero-dep fuzzy-semantic), `HybridBackend` (RRF
  fusion, zero-dep). All dependency-free; the agent is the true semantic
  engine. See current spec §6.
- **`scripts/okf-loom plan` + action envelope** — discovery → reviewable `PlannedAction`
  envelope (mechanical `argv` vs agent-decision `argv_template`). The
  `discover` → `UpdatePlan` auto-converter is built.
  See current spec §7.
- **Runtime viewer plugin loader** — entry-point discovery of `ViewerPlugin`s
  via the `okf_loom.viewer_plugins` group; two-layer active-code gate. See
  `scripts/okf_loom/viewer/OVERRIDES.md` §5 and current spec §15.

### Chosen by us

- GLiNER NER + LLM discovery (capability stubs ship; no ML dependency in core).

## Change control

When scope changes:

1. Add the new requirement as a row in the matrix above.
2. Bump `LOOM_VERSION` in `scripts/okf_loom/__init__.py` and update the current spec plus any publication notes used for the next release.
3. Add tests under `tests/` that pin the new behaviour.
4. Update `SKILL.md` and the relevant focused resource under `resources/` if the user-facing surface changes.
