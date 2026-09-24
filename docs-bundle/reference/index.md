# Reference

This quadrant is the **information-oriented** surface of the okf-loom
docs bundle. Each concept below describes the machinery neutrally — no
steps, no rationale. Read topically to find the fact you need.

For step-by-step recipes see [/how-to/index.md](../how-to/index.md); for
walk-throughs see [/tutorials/index.md](../tutorials/index.md); for
design rationale see [/explanation/index.md](../explanation/index.md).

# Concepts

| Concept | Describes |
|---|---|
| [Current toolkit specification](spec.md) | The single current contract for okf-loom 2.0.0 / OKF 0.2: metadata, migration, retrieval, Context Packs, CLI, and live studio |
| [CLI command reference](cli.md) | Every `okf` subcommand: purpose, flags, exit codes, output formats |
| [Frontmatter contract](frontmatter.md) | SPEC §4.1 keys, required vs recommended, round-trip preservation, YAML gotchas |
| [Link forms](links.md) | Absolute bundle-relative vs relative links, anchors, wikilinks, broken-link tolerance |
| [Capability registry](capabilities.md) | Core / recommended / optional tiers, the full catalogue, opt-in mechanisms |
| [Search modes](search_modes.md) | Six-mode CLI, HTTP, and portable ranking; additive passage evidence and immutable caches |
| [Agent Context Packs](context_packs.md) | Schema-v1 deterministic cited context, budgets, lines, snapshots, CLI/HTTP, and portable behavior |
| [HTTP server routes](http_routes.md) | Every live route, including strict six-mode `/__search` and schema-v1 `/__context` contracts |
| [Comment lifecycle and archive rules](comment_lifecycle.md) | State model, archive track, threading, server-side rules, JSON shape |
| [Proposal / Diff Review normative specification](proposal_diff_review.md) | Implementation-ready experimental extension contract for immutable review, deterministic preview, atomic apply/recovery, audit, undo, API/CLI, and accessible Studio UX |
| [Proposal / Diff Review decision provenance](proposal_diff_review_brief.md) | Resolved non-normative Q1–Q37 provenance retained behind the binding extension specification |
| [`okf-loom.config.yaml` reference](config_yaml.md) | `viewer.*`, `search.*`, `validate.*`, `studio.*` keys with types and defaults |

# Deep design docs (imported from `docs/`)

Long-form authoritative sources for design rationale and supporting detail.
Linked from the concise concepts above; read topically when you need depth.

| Concept | Describes |
|---|---|
| [okf-loom architecture](architecture.md) | Module-by-module developer reference (parse → model → validate → discover → update → search → render → server → studio) |
| [Embedding guide](embedding_guide.md) | Harness-specific wiring recipes (opencode, Claude Code, Codex, LangGraph, MCP, custom) |

# Conventions used in this quadrant

- One sentence per line in prose blocks.
- ATX headings (`#`, `##`, `###`) in strict order.
- Tables are the workhorse — parameters, routes, and state transitions all use tables.
- Fenced code blocks carry language tags (`bash`, `yaml`, `json`, `http`).
- Cross-links prefer the absolute bundle-relative form recommended by the
  [current okf-loom spec](/reference/spec.md) and upstream OKF 0.2 §6.1,
  e.g. `[cli](/reference/cli.md)`.
- Timestamps and version-like YAML strings are quoted
  (see [frontmatter.md](frontmatter.md) § YAML gotchas).

# Source of truth

Reference concepts describe **okf-loom 2.0.0** implementing upstream **OKF 0.2**.
The [current spec](/reference/spec.md) owns the product contract and links the
authoritative upstream source; runtime/help must match it. For implementation lookup:

1. The checkout runtime: `scripts/okf-loom --help` and per-command `--help`.
2. The library: `okf-loom/scripts/okf_loom/` (`server.py`, `studio.py`,
   `search.py`, `extensions.py`, `config.py`).
3. The supporting design docs: [`/reference/spec.md`](/reference/spec.md)
   and [`/reference/architecture.md`](/reference/architecture.md).

When a deep design doc and this quadrant disagree, the checkout runtime (`scripts/okf-loom`)
is the tie-breaker; please file or update the relevant concept.

# Where to go next

- New to okf-loom? Start with [/tutorials/install.md](../tutorials/install.md).
- Looking up a single command? Jump to [cli.md](cli.md).
- Authoring concepts? Read [frontmatter.md](frontmatter.md) and [links.md](links.md).
- Embedding the studio? Read [http_routes.md](http_routes.md) and [config_yaml.md](config_yaml.md).
