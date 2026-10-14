---
type: Reference
title: okf-loom Current Specification
description: The canonical okf-loom 2.0.0 contract for OKF 0.2 metadata, conservative migration, source-checkout distribution, retrieval, Context Packs, CLI, and live studio.
resource: /reference/spec.md
tags: [spec, current, agent-skills, reference]
timestamp: "2026-07-02T00:00:00Z"
---

# okf-loom Current Specification

This is the canonical current specification for okf-loom **2.0.0**, implementing **OKF 0.2**.
It describes current behavior directly rather than as a release-history delta.
The authoritative runtime version is whatever `scripts/okf-loom --version`
prints; this checkout reports `okf-loom 2.0.0 (SPEC v0.2)`.

Source: upstream [Open Knowledge Format SPEC.md, Version 0.2](https://raw.githubusercontent.com/GoogleCloudPlatform/open-knowledge-format/main/SPEC.md), refetched **2026-09-09**.
`SPEC_VERSION = "0.2"` is the format version; `LOOM_VERSION = "2.0.0"` is the
toolkit version. API/storage schema versions and reviewed runtime identities are
separate contracts, not mechanically bumped with either number. Numbered sections
in this file describe Loom; explicitly marked upstream sections refer to that source.
This fork is published as [okf-loom v2](https://github.com/SpatialNexus/okf-loom-v2); `main` is the supported branch.

## 1. Primary distribution and use model

okf-loom is distributed as a git-loadable source repository skill.
The primary use case is for an agent or harness to clone the repository, load the root `SKILL.md`, read focused resources under `resources/`, and run from the checkout.

The agent-facing skill resources are:

| Resource | Use |
|---|---|
| `SKILL.md` | Primary loadable skill entrypoint. |
| `resources/format-basics.md` | Read, validate, and edit one concept or learn the base format. |
| `resources/authoring.md` | Author good concepts, links, indexes, logs, and frontmatter. |
| `resources/advanced-operations.md` | Operate search, discover/plan/update, capabilities, and builds. |
| `resources/studio-agent-loop.md` | Operate the live studio and comment loop. |

The portable command form is source-checkout invocation:

```bash
okf-loom/scripts/okf-loom <command> ...
```

When already inside the repository root, use:

```bash
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom serve docs-bundle --no-open
```

Use the checked-in helper directly; do not assume an installed console script or package.
The primary distribution assumption is this git repo skill layout plus the checked-in `scripts/okf-loom` helper.

## 2. OKF bundle and concept contract

An OKF bundle is a directory tree of Markdown files.
Each concept is one non-reserved `.md` file with YAML frontmatter plus a Markdown body.
Plain tools can read it, git can diff it, and agents can parse it without an SDK.

Only `type` is always required by OKF 0.2.
okf-loom recommends but does not require `title`, `description`, `resource`, and `tags`.
Unknown frontmatter keys are legal and must be preserved round-trip.

The following API example is demonstrative, not a claim of actual generation or verification:

```markdown
---
type: API Endpoint
title: Create Order
description: POST a new order to the checkout service.
resource: https://api.example.com/v2/orders
tags: [checkout, write-api]
generated: {by: "example-agent/1", at: "2026-06-15T10:00:00Z"}
relations:
  - target: tables/orders
    type: writes_to
    detail: idempotent on client_request_id
entities:
  - id: entity/order
    label: Order
    kind: business_entity
aliases: [purchase, transaction]
citations:
  - id: "1"
    text: Internal data dictionary
---

# Overview

This endpoint writes [orders](/tables/orders.md).
```

Reserved files keep their upstream meanings.
`index.md` is a directory listing for progressive disclosure.
`log.md` is an optional append-only update history.
Only the bundle-root `index.md` may carry frontmatter, and only for `okf_version`, `okf_extensions`, and okf-loom `generated` marker.
Non-root `index.md` files are pure listings or marker-managed generated regions.

Concept links may be absolute bundle-relative or relative.
Prefer absolute bundle-relative links such as `[orders](/tables/orders.md)` because they remain stable when files move inside subdirectories.
Wikilinks such as `[[tables/orders|Orders]]` are accepted as migration/convenience body syntax and resolved into graph edges, but mutators write standard Markdown links.

See [/reference/frontmatter.md](/reference/frontmatter.md), [/reference/links.md](/reference/links.md), and [/reference/architecture.md](/reference/architecture.md) for the detailed reference contracts.

### OKF 0.2 metadata semantics

All provenance, trust, lifecycle, and computation families are optional. Their
absence never breaks core conformance; malformed present families produce soft
warnings (which `--strict` promotes for CI), not new always-required keys.

- `generated` is a concept provenance mapping: `by` is required within it;
  `at`, when present, is a real ISO 8601 datetime with an explicit UTC offset.
  The effective content-change instant uses `generated.at` only with a nonempty
  actor. Legacy `timestamp` is a fallback **only if `generated` is absent**,
  never when a present mapping is incomplete or malformed. No timezone is invented.
- `verified` accepts a list of `{by, at}` events or one bare mapping, normalized
  to a singleton list. Only valid events support derived trust: no valid event
  means `unverified`, nonhuman actors mean `machine-confirmed`, and a valid
  `human:<id>` event means `human-reviewed`. These tiers are advisory, never
  authentication, authorization, proof of correctness, or evidence of execution.
- Actor conventions are `<producer>/<version>`, `process:<id>`, and `human:<id>`.
  Record the actual producer/checker. Never invent a human or infer a historical
  producer from the agent currently editing or importing a document.
- Absent `status` means `stable`; other lifecycle values are `draft` and
  `deprecated`. `stale_after` is an absolute aware datetime: stale means
  `now >= stale_after`, including equality. Absent/invalid expiry means unknown,
  not proof of freshness. Current-time display does not rewrite authored metadata.
- Each `sources` entry requires a nonempty `resource`, which may name a URL,
  bundle path, or non-navigable population/scope descriptor. Optional stable
  `id` values join body `[^id]` footnotes by exact identity, not list position or
  footnote prose. Duplicate ids warn and do not acquire an ambiguous attribution.
  Recognized internal concept sources contribute graph links; scope descriptors
  are not guessed into paths. `author`, nonnegative integer `usage_count`, and
  aware `last_modified` are objective source signals, **not a credibility score**.
  A per-source `{from, to}` `usage_window` overrides the shared sibling window
  by presence; an invalid override does not silently use the shared value.
- `Attested Computation` describes `runtime`, typed `{name, type, required}`
  `parameters`, optional `computation` path (otherwise the body `# Computation`
  fence), `executor.resource` and `executor.receipt` field names, and
  `attester.resource`. Upstream requires `runtime` for this type within the
  optional family; Loom reports its absence as a warning. Loom **reads,
  validates, and displays metadata only**. It never fetches or executes an
  executor/attester, checks receipts, or implements an execution, sandbox,
  attestation-verdict, or invocation protocol. Upstream's execution walkthrough
  is informative, not a shipped Loom API.

The shared metadata reader feeds model, search, Context Pack, export, and viewer
projections. Live and portable views surface advisory metadata and preserve the
legacy timestamp label. Concept pages show every advisory badge; search results
show only non-default advisory states, evaluated once when the results render
(see the search result rules in the viewer surface below). Additive live
search/context `metadata` does not change schema-v1 exact excerpts, physical
line citations, or ranking contracts.
Context Markdown includes a separate normalized metadata/source JSON data block,
charged before item selection in addition to the existing evidence budget
projection; see [Context Packs](context_packs.md#token-budget-counts-and-truncation).
Metadata cannot bypass the budget or synthesize body footnote definitions.
The strict portable passage schema remains unchanged; optional concept-inventory
metadata supplies portable presentation while legacy inventories remain readable. Loom `provenance`/`citations`
extensions, legacy body `# Citations`, OKF `sources` footnotes, and exact Context
Pack `[path.md#Lx-Ly]` citations remain distinct and are not auto-converted.

Only literal boolean `generated: true` on the **root index** opts into whole-body
index regeneration when no markers exist. A concept-style `generated` mapping,
string `"true"`, or other truthy value never confers that ownership. Paired index
markers continue to own only their enclosed region.

## 3. Core runtime invariants

okf-loom is a Python library and CLI over the same model objects: `Bundle`, `Concept`, `Link`, `Graph`, and `ContentIndex`.
The `ContentIndex` is the shared model for graph, search, listings, viewer, and build outputs.

Hard invariants:

- Implement upstream OKF 0.2 while retaining documented 0.1 compatibility.
- Do not introduce hard runtime dependencies beyond okf-loom baseline.
- Load permissively and preserve unknown data.
- Write through parse-modify-serialize paths.
- Use atomic writes through temporary file plus rename.
- Keep outputs deterministic; tie-break by concept id where ranking or ordering is otherwise equal.
- Keep machine-readable output JSON-serializable.
- Never overwrite hand-curated `index.md` or `log.md` content silently.

When a live studio session is attached, concept writes route through the studio write funnel so they are attributed, undoable, logged to the change feed, and broadcast live.
Without a live session, mutators use the same data-safety principles through the non-studio atomic write path.

## 4. Capability registry and governed extension keys

The capability registry gives meaning to optional keys without changing the base spec.
Bundles opt in by declaring `okf_extensions:` in the bundle-root `index.md` or by using governed keys that auto-activate the corresponding capability.

The active tier model is:

| Tier | Meaning |
|---|---|
| `core` | Always active format primitives such as frontmatter, links, indexes, logs, lexical search, graph, backlinks, listings, and mention discovery. |
| `recommended` | Ships with okf-loom and requires no plugin or extra. |
| `optional` | Recognised extension surface that may need external provider, plugin, or explicit opt-in. |

Current governed top-level keys include `relations`, `entities`, `aliases`, `provenance`, and `citations`.
okf-loom intentionally uses top-level governed keys rather than a single nested vendor container.
Unknown keys are opaque and preserved.

Example root declaration:

```yaml
---
okf_version: "0.2"
okf_extensions:
  - okf.cap.typed_relations
  - okf.cap.entities
  - okf.cap.aliases
generated: true
---
```

Run capability inspection with:

```bash
scripts/okf-loom capabilities
scripts/okf-loom capabilities --bundle docs-bundle --format json
```

See [/reference/capabilities.md](/reference/capabilities.md).

## 5. Validation, profiles, and configuration

Default OKF conformance is intentionally small.
A bundle is conformant when non-reserved concept files have parseable YAML frontmatter with non-empty `type`, and reserved `index.md` / `log.md` files follow their structural rules.
Broken links, missing recommended keys, unknown types, and undeclared capabilities are guidance by default.

### Format versions and upgrade diagnostics

The root `index.md` may declare `okf_version`; omission remains legal and assumes
the current format `0.2`. Version diagnostics parse the complete ASCII
`major.minor` value, not a numeric prefix. Surrounding whitespace and leading
zeroes are tolerated; comparison is numeric. Quote version strings to avoid
YAML numeric coercion (existing unquoted numeric scalars remain readable).
Bare majors, patch components, prerelease/build suffixes, nonversions, and
present-but-empty/null declarations are malformed, not older or compatible.

Validation emits `spec.version_malformed` as a warning with compatibility unknown,
while continuing best-effort consumption. Absence emits
`spec.version_undeclared` at INFO; valid newer minor/different major declarations
warn, and valid older minors emit INFO. These findings add no required metadata
and do not by themselves make default validation fail; `--strict` fails on
warnings as usual.

`upgrade <bundle>` defaults to a conservative read-only preview; `--preview` and
`--check` also create no locks, directories, caches, indexes, or logs. `--apply`
opts into the supported 0.1 → 0.2 migration. It preserves legacy evidence and
authored prose, adds generation metadata from valid timestamps only with an
explicit original-producer `--actor`, and writes the root declaration last.
Undeclared bundles are not relabeled; declared 0.2 bundles can finish deferred work.
`--expected-revision` optionally binds apply to a reviewed preview snapshot.
Apply requires Linux descriptor-relative `RENAME_EXCHANGE`, with no ordinary
rename fallback. Source parents are pinned no-follow; inode/metadata/bytes and
the actually displaced source are verified around exchange. Attempt ownership
is recorded before the syscall, so a writer raising after rename—including a
directory-open/fsync failure or `KeyboardInterrupt`—still enters reconciliation.
Caught failures restore actual displaced entries only while ownership can be
verified. Newer canonical edits and uncertain exchange evidence are preserved;
`upgrade.rollback_uncertain` requires manual source/evidence inspection.
`upgrade.cleanup_uncertain` distinguishes a published batch with uncertain cleanup.
Private `.okf-upgrade-<filename>-<nonce>/entry` slots remain until settlement and
may survive interruption. Inspect all surviving slots, including in a moved
original parent, before forward rerun with the same actor and a fresh revision.
This is atomic exchange with verification, **not** kernel conditional CAS or a
bundle-wide transaction. Readers may observe a candidate before verification;
repeated noncooperating writes, hostile same-UID staging access and hard process/
power loss have no universal automatic-restoration guarantee. No migration
journal, log or Studio history is fabricated. Exit zero can include
`status: partial`; inspect `deferred`.
Unsupported/malformed input or conflict exits `1`; CLI usage errors exit `2`.
No-argument `upgrade` reports versions. Plain `upgrade <bundle>` retains one
informational compatibility exception: a valid unsupported source version exits
zero (`status: unsupported` in JSON), with no conversion plan. Explicit modes or
target/actor options request support checking and refuse it. See [upgrade CLI details](/reference/cli.md#upgrade)
and the [migration procedure](/how-to/migrate_spec_version.md) before applying.

### Validation profiles

| Profile | Behaviour |
|---|---|
| `spec` | Default; only hard OKF 0.2 conformance errors fail. |
| `producer` | `spec` plus missing recommended concept keys promoted to errors. |
| `loose` | Permissive migration mode; missing `type` becomes a warning. |

Useful validation commands:

```bash
scripts/okf-loom validate docs-bundle
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom validate docs-bundle --profile producer
scripts/okf-loom validate docs-bundle --fail-on-broken-links
scripts/okf-loom validate docs-bundle --format json
```

`okf-loom.config.yaml` is an optional bundle-root config file.
It is additive; absence means defaults.
Recognised sections are `bundle`, `viewer`, `search`, `validate`, and `studio`.
Unknown config keys are preserved in `OkfConfig.raw` for forward compatibility.
Path-bearing fields must be bundle-relative and must not escape the bundle root.
Boolean and enum values fail closed rather than guessing.

### Bundle scanning and exclusions

Markdown discovery (`Bundle.load`, the serve watcher, and `okf import`) walks
the bundle tree top-down and prunes excluded directories rather than
rglob-scanning everything, so a bundle root that is also a real workspace
root does not sweep `node_modules`, nested cloned repos, or virtualenv trees
in as thousands of missing-`type` pseudo-concepts.

Three exclusion rule groups apply; a match in a later group overrides
earlier ones, and within a group the last matching rule wins (git
semantics):

1. Built-in defaults: hidden directories (`.git`, `.okf-loom`, `.venv`, …),
   `node_modules/`, `__pycache__/`, `venv/`, `bower_components/`. Hidden
   *files* still load — only hidden directories are pruned. Additionally,
   any non-root directory containing a `.git` entry (a nested cloned repo,
   submodule, or worktree) is pruned — a structural rule, overridable only
   by `bundle.include`.
2. `.gitignore` rules — the bundle root's file and nested ones, each scoped
   to its own directory — unless `bundle.respect_gitignore: false`.
3. `bundle.exclude` patterns from `okf-loom.config.yaml` (a negation such
   as `"!.docs/"` re-includes a path the defaults or a `.gitignore`
   excluded, with git's limitation: not from under a pruned directory).

Above all exclusions sit `bundle.include` patterns — the explicit add-back
lever. Include beats every exclusion source, including the structural
nested-repo rule, and CAN reach inside pruned directories. Two forms:

* Directory form (`"vendor-repo/"`, or a wildcard-free path like
  `"node_modules/my-pkg/docs"`): the directory is revived — the scanner
  descends its ancestors just far enough to reach it, then scans the
  subtree as normal bundle content. Exclusion rules that were overridden at
  the revived directory (an anchored `node_modules/**` in a `.gitignore`, a
  `bundle.exclude` that killed it) stay overridden for its contents, while
  unrelated rules — the hidden-dir default, basename rules like
  `*.tmp.md`, excludes targeting deeper paths — keep applying inside. This
  is the recommended form.
* Glob form (`"vendor-repo/**"`): force-includes exactly what it matches,
  everything beneath — defaults included. A broad `**` on a huge tree is a
  deliberate, paid-for traversal.

Include patterns without a `/` (basename form) apply wherever the scan
already reaches but never open pruned directories — pulling something out
of a pruned tree requires naming its path. Include patterns are positive
only; a `!` there is rejected fail-closed.

Patterns use the practical gitignore subset: `*`, `?`, and character
classes never cross `/`; `**` spans path segments; a trailing `/` makes a
rule directory-only; a `/` anywhere else anchors it to the rule's base
directory; leading `!` negates (exclusion groups only). Scanning stays
deterministic (name-sorted per directory) and never follows symlinked
directories. An unparseable config keeps the bundle loadable: loading warns
(`config.unparseable`) and scans with the built-in defaults.

See [/reference/config_yaml.md](/reference/config_yaml.md) and [/how-to/validate_in_ci.md](/how-to/validate_in_ci.md).

## 6. Search modes and zero-dependency retrieval

The current shipped search story is dependency-free.
Do not describe dense embeddings, vector indexes, SQLite FTS provider flags, provider config keys, or semantic/LLM package extras as shipped current behavior.

The six current modes are:

| Mode | Backend / source | Notes |
|---|---|---|
| `lexical` | `LexicalBackend` | Default BM25-style field-weighted keyword search. |
| `tag` | `LexicalBackend` | Case-insensitive tag membership. |
| `semantic` | `SemanticLiteBackend` | Fuzzy-semantic token + character-trigram cosine; not embeddings. |
| `hybrid` | `HybridBackend` | Reciprocal Rank Fusion of lexical and SemanticLite. |
| `entity` | In-module entity matcher | Matches concept ids, titles, descriptions, entity labels, and aliases. |
| `relation` | In-module relation/edge filter | Filters `relations:` and Markdown graph edges by relation/source/target. |

Examples:

```bash
scripts/okf-loom search docs-bundle "comment lifecycle"
scripts/okf-loom search docs-bundle "agent loop" --mode semantic
scripts/okf-loom search docs-bundle "studio" --mode hybrid
scripts/okf-loom search docs-bundle "#reference" --mode tag
scripts/okf-loom search docs-bundle "" --mode relation --relation depends_on
```

CLI, live HTTP, and JavaScript-enabled portable builds expose all six modes.
`type` and `tag` filter every mode; `entity` applies only to entity mode, and
`relation`, `source`, and `target` apply only to relation mode.
Unknown, duplicate, malformed, out-of-range, and mode-inapplicable inputs fail
closed rather than selecting a fallback ranking algorithm.

Search results preserve the compatibility fields and add bounded ranking
reasons, backend identity, and schema-v1 exact-passage evidence.
The live `GET /__search` route defaults to lexical mode and limit 20, accepts
all six modes, and caps the limit at 100.
Its JSON success remains a bare result array for compatibility.

`GET /__search-form` is a read-only adapter for native HTML GET forms, not
another retrieval API. It accepts scalar `q`, `mode`, `type`, `tag`, `entity`,
`relation`, `source`, `target`, and `limit`. It normalizes form-encoded slashes,
omits empty optional filters and mode-inactive entity/relation filters, and
returns an empty `303` response whose `Location` is canonical `/__search`.
Unknown or duplicate fields and malformed encoding are rejected through the
strict search error path; query/mode/limit validity is enforced by the redirected
endpoint. Direct `/__search` requests remain strict, including explicit empty
or mode-inapplicable filters. The adapter never writes bundle or session state
and is available on the dedicated public read-only ingress.

HTML search errors retain unambiguous submitted controls for correction, escaped
as display-only input rather than accepted retrieval state. JSON errors retain
their typed envelope. Busy/timeout HTML uses a minimal recovery form without
accessing the busy bundle's derived data; the timed-out worker retains its slot
until it actually exits, but cannot delay or overwrite the terminal response.

`context` and `GET /__context` produce deterministic schema-v1 Agent Context
Packs from a query, canonical concept, or exact selection seed.
Packs contain exact source excerpts, absolute physical line coordinates,
stable citations, deterministic revision/item ordering, bounded graph
neighbors, explicit included/omitted/truncated counts, and canonical JSON and
Markdown representations over the same item set.
They are read-only and operate over one immutable bundle snapshot.

Static, SPA, and single-file builds with JavaScript use a versioned,
revision-bound corpus for all six modes and context construction.
Live no-JavaScript GETs remain complete; static no-JavaScript pages provide
browse and prebuilt cited concept context, but not arbitrary retrieval.
See [/reference/search_modes.md](/reference/search_modes.md) and
[/reference/context_packs.md](/reference/context_packs.md).

## 7. Discovery, planning, update, repair, and authoring verbs

okf-loom supports both report-oriented and action-oriented workflows.
`discover` finds gaps and suggestions.
`plan` builds a reviewable action envelope.
`update` applies an update plan or action envelope idempotently.
`repair` applies mechanical fixes such as marker-safe index regeneration and typed-relation mirroring.

```bash
scripts/okf-loom discover docs-bundle --out /tmp/okf-suggestions.json
scripts/okf-loom plan docs-bundle --out /tmp/okf-plan.json
scripts/okf-loom update docs-bundle --plan /tmp/okf-plan.json --dry-run
scripts/okf-loom repair docs-bundle --indexes # dry-run by default; --apply writes
```

Scoped work is supported for fast agent loops:

```bash
scripts/okf-loom plan docs-bundle --scope reference/spec --neighbors --out /tmp/scoped-plan.json
scripts/okf-loom update docs-bundle --plan /tmp/scoped-plan.json
```

Authoring verbs are direct, safe mutators:

```bash
scripts/okf-loom write-concept --bundle docs-bundle --id reference/new_topic --type Reference --title "New Topic"
scripts/okf-loom set-frontmatter --bundle docs-bundle --id reference/new_topic --key tags --value '["reference"]' --json-value
scripts/okf-loom link-add --bundle docs-bundle --source reference/new_topic --target reference/spec --label "current spec"
scripts/okf-loom entity-add --bundle docs-bundle --id reference/new_topic --label "okf-loom" --kind Product
printf '%s\n' 'exact old text' > /tmp/frag.md
scripts/okf-loom update-section --bundle docs-bundle --id reference/new_topic --heading "## Usage" --body-file /tmp/frag.md --create-if-missing
scripts/okf-loom replace-text --bundle docs-bundle --id reference/new_topic --old "exact old text" --new "exact new text"
```

`link-add` is fail-closed on missing targets unless `--allow-forward-reference` is passed intentionally.
Repeated actions should converge to no-op or already-present results rather than duplicate content.

For plan-based authoring verbs and `update`, zero applied operations with a
hard failure return CLI exit `1`. This includes `rev_conflict` and
`studio_blocked:<reason>` from the Studio write funnel; a constraint denial is
not a successful no-op. Per-operation reasons remain in JSON results. Genuine
idempotent/protective no-ops retain exit `0`, and any applied operation retains
the existing partial-success exit `0`. This reporting rule does not change
transaction rollback or constraint enforcement.

Partial body updates are first-class: `update-section` replaces (or, with
`--append`, appends to) exactly one section — matched by heading text,
optionally level-pinned (`"## Banking"`), including its subsections — and
`replace-text` applies an exact textual patch. Both mutate only the body,
fail closed on a missing target (`section_not_found` / `text_not_found`)
and on ambiguity (`section_ambiguous:N` / `text_ambiguous:N`), and are
available as `/__apply` op kinds `update_section` / `replace_text`. An
agent should never need to reconstruct a whole document (or stage a copy)
to change one block.

`write-concept` CREATE defaults `resource` to the bundle-absolute concept path
and `generated` to `{by: okf-loom/2.0.0, at: <current UTC instant>}` for newly
produced content. Explicit legacy `--timestamp` suppresses the generation
default rather than guessing its historical actor. UPDATE never injects these
defaults; `--no-defaults` opts out. Content keys (title, description, tags) are
never invented, so supplying them is still necessary for a warning-free strict
run. Imports preserve typed authored bytes and never infer generation or verification.

## 8. Indexes, logs, scaffolding, and derived artifacts

`index` regenerates directory listings without clobbering hand-authored content.
It rewrites only tool-managed root indexes or marked regions delimited by `<!-- okf:generated:index begin -->` and `<!-- okf:generated:index end -->`.

`log` appends to `log.md` newest-first and preserves existing entries.

`scripts/okf-loom init` scaffolds a new bundle with `index.md`, `log.md`, and `okf-loom.config.yaml`.
`scripts/okf-loom bootstrap` scaffolds a simpler bundle without config.
Both refuse unsafe destructive assumptions.
New scaffolds declare `okf_version: "0.2"`.

Derived JSON indexes are optional and disposable:

```bash
scripts/okf-loom index docs-bundle --emit-json
```

This writes generated artifacts under `<bundle>/.okf-loom/index/` such as `content.json` and `graph.json`.
They contain no absolute host paths, carry `generated_by: "okf-loom"` and `format_version: 1`, and should be safe to delete and regenerate.
Bundles should normally gitignore `.okf-loom/index/` and `.okf-loom/session/`.

## 9. Viewer, build, and static outputs

All viewer targets share the same `ContentIndex`.

| Target | Command | Use |
|---|---|---|
| Live studio / server | `serve` | Interactive browsing, comments, agent loop, SSE updates. |
| SPA build | `build --target spa` | Published rich reading experience. |
| Static build | `build --target static` | JS-optional, printable, SEO/air-gapped output. |
| Single-file build | `build --target single-file` | Portable artifact for tickets, email, or local sharing. |
| One-file render | `render` | Self-contained viewer artifact. |

Examples:

```bash
scripts/okf-loom serve docs-bundle --no-open
scripts/okf-loom serve docs-bundle --no-open --tunnel   # + public https URL (cloudflared)
scripts/okf-loom build docs-bundle --target static --out /tmp/okf-docs-site
scripts/okf-loom render docs-bundle --out /tmp/viz.html
```

**Ordinary build destination safety.**

For all `build` targets, the output directory must not equal or contain the
bundle source, or contain any loaded source concept, index, or log document.
Admission is read-only and precedes build writes. Destination paths containing
symlinks (including dangling links and symlinked parents) or non-directory
components are refused. Use a real, caller-controlled output directory and
parent; do not allow non-cooperating processes to rename or replace those
paths during a build.

An output such as `<bundle>/dist/` remains supported. Multi-file builds copy
the §5-visible media set except files resolving inside the selected output or
the current staging tree, preventing generated media from nesting on rebuild.
This does not alter `Bundle.load` visibility. Use existing `.gitignore` or
`bundle.exclude` rules to keep other generated trees and retained recovery
out of later source scans; explicit `bundle.include` can override those rules.

**Output ownership policy.**

`build --output-policy replace` is the default and is unchanged: `static` and
`spa` replace the entire selected output directory, including an existing
nonempty output, and read or write no ownership manifest. `single-file` retains
its narrower behavior: atomically replace `index.html` inside the selected
directory, leaving other files there.

`--output-policy managed` is an explicit opt-in for `static` and `spa` only.
`single-file` never owns its directory, so `--target single-file --output-policy
managed` (with or without `--adopt-output`) exits `2` before loading the bundle
and writes nothing. `--adopt-output` without `--output-policy managed` also
exits `2` and writes nothing. A managed build publishes `.okf-export.json` at
the output root, inside the same directory rename as the tree it describes.

The manifest schema (version 1) is closed. Its bytes must equal the canonical
encoding: UTF-8 JSON with sorted keys, `,`/`:` separators, non-ASCII
characters unescaped, and one trailing newline. Duplicate keys, extra
whitespace, alternative number forms, and a BOM are therefore malformed. The
top level has exactly `schema: "okf-loom/export-ownership"`,
`generator: "okf-loom"`, `version: 1`, `target` (`static` or `spa`),
`loom_version`, `bundle_revision`, and `entries`. A directory entry is exactly
`{path, type: "dir"}`. A file entry is exactly
`{path, type: "file", size, sha256}`, with an integer size and 64 lowercase hex
digits. Paths are relative and `/`-separated, with no empty, `.`, or `..`
segment. They never name the manifest itself, are sorted by UTF-8 bytes and
unique, and every parent directory is listed. The manifest is at most 16 MiB and
200,000 entries. It records every published file and directory, but no modes,
mtimes, absolute paths, bundle location, or user. `target`, `loom_version`, and
`bundle_revision` are informational. The manifest is published with the site.
A version other than the integer `1` is `manifest_unsupported`.

A managed build classifies the destination:

| Destination | `managed` | `managed` + `--adopt-output` |
|---|---|---|
| Absent, or an empty directory | Create. | Create. |
| Manifest is a symlink, special file, or directory; or is malformed, oversized, or an unsupported version | Refuse. | Refuse; adoption never overrides a present manifest. |
| Valid manifest; every entry present is listed and byte-identical, and nothing listed is missing | Replace. | Replace. |
| Valid manifest; unknown file or directory | Refuse `entry_unknown`. | Same. |
| Valid manifest; a listed entry is missing, or its kind, size, or bytes changed | Refuse `entry_modified`; a deletion counts as an edit. | Same. |
| Symlink or special file anywhere in the tree | Refuse `entry_symlink` / `entry_special`. | Same. |
| Nonempty, no manifest | Refuse `unmanaged_nonempty`. | Adopt only if the tree equals the fresh build byte for byte (ignoring the new manifest); otherwise refuse `adopt_mismatch`. |

Switching a managed output between `static` and `spa`, or rebuilding it from a
different bundle, is allowed: ownership belongs to the tree, not the bundle. A
hash only proves bytes are unchanged since the manifest was written. No hash,
generator, or version field grants authority by itself. Replacement is
authorized by three things together: the operator's explicit flags for this
destination; filesystem write permission (anyone able to forge the manifest can
already delete those files); and proof that replacement discards only okf-loom
output (managed) or loses no information at all (adoption).

Inspection never follows symlinks and never opens special files. Files are
opened no-follow and non-blocking, then re-checked by descriptor. Three checks
run:

1. A write-free preflight before any directory creation, staging, or rendering.
   A present manifest and a managed tree are judged in full here. For an
   adoption candidate (nonempty, no manifest) the preflight checks structure
   only: symlinks, special files, names, and the entry limit.
2. An authoritative full re-hash under the publication lock, before any
   recovery write. `adopt_mismatch` is judged here against the sealed staging
   inventory, after rendering. An adoption candidate file that cannot be opened
   (for example, unreadable) is also first refused here, as `entry_modified`.
3. After the old tree has moved into private recovery, a metadata recheck of
   the root directory's device and inode, the entry set, each file's (including
   the manifest's) device, inode, size, mtime, and ctime, and each directory's
   device and inode. Any change rolls the old tree back.

Running out of file descriptors or memory during inspection is an ordinary
error (exit `1`), not an ownership finding. Inspection holds at most one
directory descriptor per level of tree depth, however wide the tree is.

A refusal leaves the destination byte-identical and removes the private
staging. Two limits remain. A non-cooperating writer can still change the
quarantined tree after the last check: through descriptors it opened earlier,
or by path under `.okf-recovery-*/previous` when it runs as the same user
(mode `0700` does not restrict the owner). A writer able to change
the clock could mask a same-inode rewrite between the last two checks. Hashing
runs twice, once while holding the parent lock, so large managed sites block
sibling publishers for longer.

Refusals exit `2` with
`error: export.ownership.<code>: <out> (<n> finding(s); first: '<path>'); no files changed`.
The codes are `unmanaged_nonempty`, `adopt_mismatch`, `manifest_malformed`,
`manifest_unsupported`, `entry_unknown`, `entry_modified`, `entry_symlink`, and
`entry_special`. Messages never include hashes or file content. Managed builds
report `output_policy: managed` and
`ownership: created|replaced_managed|adopted`, and their `files` count includes
the manifest. Legacy build output and stats are unchanged.

Adoption requires byte-deterministic builds. Repeated builds of an unchanged
bundle are byte-identical, but some output honestly depends on time or session
state. Freshness badges compare `stale_after` with the build clock, and exports
can embed a recent-change snapshot from session history. When either changes
between the original build and the adoption run, adoption refuses with
`adopt_mismatch`. That is fail-closed by design. A `replace` build into a
managed directory drops the manifest, so the next managed run needs
`--adopt-output`.

Multi-file staging is private and destination-local, including when missing
parents must first be created. Publication uses same-filesystem renames and a
unique private `.okf-recovery-*` directory for prior output, never `dst.old`.
Unrelated `.old` files, directories, and symlinks are untouched. An advisory
lock on the pinned destination parent serializes cooperating tree publishers
across threads/processes (including sibling outputs). The two renames are not
an atomic directory exchange: readers may briefly see no destination. This
ordinary-build contract is not the capture/proposal publication contract and
does not promise crash durability.

If publication fails, prior output is restored when possible. If restoration
cannot be confirmed, or backup cleanup fails, the operation reports a recovery
error naming the retained path rather than silently deleting recovery. Later
builds, including managed builds, never adopt or clean that path. A managed
build that finds the destination absent after an interrupted publication
creates it and leaves the recovery for inspection. After a power loss, a torn
manifest is refused as `manifest_malformed`, and a missing manifest leaves a
nonempty destination `unmanaged_nonempty`. Stop competing writers and inspect both
the destination and reported recovery before manually restoring or removing
anything; do not blindly retry or delete recovery. Process termination can
also leave staging/recovery directories requiring inspection. Successful
multi-file return values describe the generation just built.

Portable static and single-file outputs omit live comment state by default.
Optional `viewer.review_base_url` in `okf-loom.config.yaml` configures a
credential-free HTTP(S) Studio deployment base, including an optional path prefix.
Exports expose `#okf-review-bootstrap` JSON: `{schema:1, base_url, document, url,
read_only:true, message}`. Static document pages carry their canonical `.md`
path and review URL; single-file/graph/search shells carry the base with a null
document/URL so the client can select the current document and append its fragment.
The message explains that comments require live Studio even when no base is set.
No session token, comment body, upload, or embedded comment backend accompanies
this metadata. Queries/fragments (including empty delimiters), userinfo,
non-HTTP(S), percent escapes, backslashes, whitespace/control characters,
non-ASCII and dot-segment/ambiguous paths in configured bases are rejected.
Configured bases are normalized to one trailing slash. Document fragments are
separately percent-encoded when constructing review links.

The viewer presents that metadata as **Review in Studio** inside the export's
read-only note (`.okf-static-viewer-note`), which every exported page carries,
including reserved log pages. With a base, the note gains one ordinary same-tab
link whose destination equals `review_url(base, document, fragment)` byte for
byte: the page's own document on concept, index and log pages; the selected
concept or the reserved document being read in the graph and single-file
viewers; and the bundle home when nothing is selected or the single-file concept
list is showing. The fragment is the address-bar hash only when it names an
element of the document being read, never viewer chrome. The destination
document and host are spelled out beside the link, its accessible name is
`Review in Studio: <document>[, section <fragment>]`, and it sends no referrer.
The client re-admits the base with the same rules as configuration, so a
hand-edited export cannot mint a credentialed, query-bearing or non-HTTP(S)
destination. Without an admitted base the note states that Review in Studio is
unavailable and why; no disabled control is shown and the export stays fully
readable. The note is a JavaScript enhancement like the rest of that banner.

The single-file concept list is a real destination: each entry is a link
(`?selected=<id>#detail-content`) that selects the concept in place when the
viewer script runs, moves focus to the concept content, and returns to the list
with one Back step. While the list is showing, the page scrolls as a document
rather than inheriting the graph's viewport-bound shell. In the graph detail
pane a link to a fragment of the concept already shown (for example a claim
source footnote) stays a same-page jump, so its disclosure opens and its target
takes focus instead of the concept being re-selected without the fragment.

Graph membership is concept-only. Existing index/log links are kept in
`Graph.navigation`, not in concept edges or unresolved counts. Missing documents
and missing rendered heading fragments remain unresolved. Graph JSON includes a
separate `documents` map for reserved-document reading; these are not nodes.
Rendered graph bodies use canonical bundle-absolute `.md` links resolved from
the selected source, including sibling, `./`, `../` and heading fragments.
Portable clients map those identities to their own destinations, rather than
resolving relative paths against the graph shell. Static builds emit logs and
authored indexes even in directories without concepts; serving never expands
beyond the selected visible bundle.

An explicit `--include-comment-summary` opt-in embeds only a locked snapshot of
per-concept active-comment-row counts. The portable `CommentSummaryV1`
authority has shape
`{schema:1, semantics:"active-comment-row", counts:{"tables/orders":2}, summary_revision:"<sha256>"}`.
It counts latest-row-per-comment open/claimed, non-archived rows (including
replies) for graph concepts. It never carries bodies, anchors, comment IDs,
row tokens, lifecycle detail, or event cursors. Corrupt or unavailable session
authority fails the opted-in build rather than fabricating zero counts. SPA
builds do not accept this snapshot flag; live Studio graph data reads the same
sanitized summary from the existing Studio authority. Checkpoints record the
latest incorporated rotation generation, so retained older generations cannot
overwrite checkpoint rows. Authority reads require the existing writer-owned
lock and use no-follow regular-file checks; they create no session paths.
Portable outputs use only the embedded snapshot and never fetch comment state.
Authoritative session event history is a separate, read-only activity source:
static and single-file outputs include a bounded, normalized recent-change
snapshot by default when that history exists. Missing authority omits the rail;
it never fabricates activity, creates session paths, mutates session state, or
performs a runtime network request. This default does not opt portable output
into comment counts.

An explicit `--include-portable-feedback` opt-in (static and single-file only;
SPA builds refuse it) adds the offline page-level note composer whose note
format is defined in §12.2. It is off by default, and default output is
byte-for-byte unchanged: `portable-feedback.js` is not part of the default
`__static` copy and `serve` does not serve it. At build time each concept's
canonical document is read with `canonical_source.read_canonical_document_bytes`.
The page is bound to the SHA-256 of those exact bytes only when they decode as
strict UTF-8 and, after universal-newline normalization, equal the loaded
`raw_text`. Otherwise the page gets no composer, stderr receives
`warning: portable feedback skipped for <document>: <reason>`, and the skip is
counted in the build stats (`portable_feedback_pages`,
`portable_feedback_skipped`); the build does not fail. A static concept page
carries
`<script type="application/json" id="okf-portable-feedback">{"version":1,"document":…,"source_sha256":…}</script>`
and loads `__static/portable-feedback.js`. A single-file output carries one
`{"version":1,"documents":{"<concept id>":{document,source_sha256}}}` map and
inlines the script, which targets the concept on screen and never a reserved
document or the concept list. The composer is a native modal dialog with a
required note, an optional quote and name (both empty at first), and an
**Include time** checkbox that is off by default; name and time are labelled as
unverified claims. **Download note** saves the JSON through a local Blob
download and **Copy JSON** uses the clipboard or shows a selectable manual
copy. A `note_id` is reused only while the content is unchanged. The draft
lives only in memory: closing an undownloaded draft asks first, and leaving the
page raises the browser's leave warning. The script makes no network request,
uses no storage, cookies or URL state, and carries no Studio address or
credential. Its limits mirror `portable_feedback.parse_note`, which stays
authoritative on import.

**Offline renderer assets (`--assets`).**

`build` (every target) and `render` accept `--assets {cdn,offline}`. `cdn` is
the default. Its output is unchanged except for one deliberate difference:
`renderers.js` (copied to `__static/`, and inlined in single-file and `render`
HTML) carries the dormant offline loader, so those bytes differ from output
built before offline assets existed. In `cdn` mode pages load Mermaid,
highlight.js, KaTeX, and the Cytoscape graph chain (Cytoscape.js, layout-base,
cose-base, cytoscape-fcose, dagre, cytoscape-dagre) from pinned
`cdn.jsdelivr.net` URLs, with SRI where the code pins it. `serve` always uses
the CDN mode. On live, static and SPA graph pages the external classic graph
vendor chain is deferred in the dependency order above, before deferred
`graph.js`; inline extensions cannot assume its globals during parsing.
`theme.js` remains parser-blocking for first-paint appearance. Single-file
output retains the parser-blocking vendor chain before its inline graph runtime.

`--assets offline` is an explicit opt-in that ships byte-verified copies of the
same pinned versions instead. The copies live in the checkout at
`scripts/okf_loom/viewer/vendor/`, and `vendor.lock.json` pins each file's
size, SHA-256, and SHA-384, plus the npm tarball SHA-512 it came from. Before
anything is written, the build checks every file it uses against the lock and
every copy of a CDN asset against the code's SRI. It also re-derives two files
and compares them with their pinned SHA-256: a highlight.js bundle that
registers the same 193 languages as the CDN ES-module build, and a KaTeX
stylesheet with its woff2 fonts inlined as `data:` URLs. The vendor folder
itself must be a real directory, not a symlink: it is opened once without
following links, and the lock and every file are read below that one directory
through no-follow descriptors, so a path swapped during the build cannot
redirect them. Published auxiliary files (`THIRD-PARTY-NOTICES.txt`,
`BUNDLED-DEPENDENCY-NOTICES.txt`, both required, and any further notice file)
must be listed in the lock's `auxiliary` manifest with
size, SHA-256, and SHA-384, and are verified the same way. A mismatch, a
missing or unlocked file, a symlink at any level (the vendor folder included),
or a lock path escaping the vendor folder fails the command
with `error: offline asset integrity check failed: <reason>; no files changed`
(exit `1`), and any previous output stays unchanged.

Static and SPA outputs copy the assets to
`__static/vendor/<package>-<version>/` with each library's `LICENSE`, plus
`__static/vendor/THIRD-PARTY-NOTICES.txt` and
`__static/vendor/BUNDLED-DEPENDENCY-NOTICES.txt`. The second file reproduces
the full licence text of every third-party component bundled inside a vendored
file (for example, the packages inside `mermaid.min.js`) or adapted into one,
with its exact version, where it is included, and the evidence for both. Each page's CSP is replaced by a
CDN-free policy (`script-src 'self'`, `font-src 'self' data:`), and each page
carries
`<script type="application/json" id="okf-asset-bootstrap">{"delivery":"files","mode":"offline","schema":1}</script>`.
Single-file and `render` outputs embed the assets as inert
`<script type="application/json" id="okf-vendor-*">` carriers, with `delivery`
set to `inline` and a CSP whose `font-src` is `data:` only. The graph chain is
always embedded. Mermaid, KaTeX, and highlight.js carriers are embedded only
when some body contains a Mermaid diagram, math, or a fenced code block. The
two notices files and the licence texts are embedded as
`<script type="text/plain" id="okf-third-party-notices">`. The bootstrap and
the carriers are generated into `<head>`, and the loader recognizes only a
`<script type="application/json">` there. Body elements, such as a heading
whose slug is `okf-asset-bootstrap`, never switch a page to offline mode or
supply a carrier, so a `cdn` build stays on the CDN; a malformed generated
bootstrap fails closed with `offline-asset-unavailable`. For the graph chain,
offline mode takes precedence over `cdn: false` in
`.okf-loom/viewer/config.json`. Offline output makes no
third-party network request, and nothing falls back to the CDN: if a local
asset is missing or unreadable, that renderer fails with the code
`offline-asset-unavailable`, and the authored source stays visible, as it does
after any renderer failure.

Offline mode never combines with active code. Overrides or plugins could change
the asset set, so it could no longer be verified. `--assets offline
--allow-active-code` exits `2` before loading the bundle. When the effective
active-code gate (§14) is open through `OKF_LOOM_ALLOW_ACTIVE_CODE` and the
bundle's `viewer.allow_active_code`, the command exits `2` with
`no files changed`. The Python API applies the same rule to an explicit
plugin: `build_site(..., assets="offline", plugin=...)` raises
`OfflineAssetsError` before the plugin runs or any output is created.

Offline output is larger: static and SPA builds add about 6.6 MB of vendored
files, and a single-file output with every renderer grows by about 7.1 MB. A
single-file offline output works fully from `file://`. A static offline output
loads every library (the renderers and the graph chain) from its own files
under `file://`, but its graph data and search corpus are fetched, so the graph
and search need an HTTP server, exactly as in CDN mode. SPA output uses
root-absolute `/__static/...` paths, so in either mode it needs an HTTP server
that serves the site at its root; under `file://` nothing, libraries included,
loads.

Maintainers verify the vendor tree with `scripts/fetch_vendor_assets.py`
(`--check`, the default, needs no network and also verifies the auxiliary
manifest). `--fetch` re-downloads it from the npm registry and verifies it, and
`--init-lock` rewrites the lock. Both network modes contact only the registry
host and refuse a redirect to any other host. They stream each tarball under
member-count, member-size, and total-expansion limits checked from the headers
before a body is read, reject duplicate member names, keep only the locked
files, and write nothing until every package verifies. A version bump is one
reviewed change that updates the code's pinned URLs and SRI values, runs
`--init-lock`, and establishes the exact licence provenance of what ships: each
`LICENSE` comes from the pinned tarball, and dependencies bundled inside a
library are identified from that exact version's build, not from current
registry metadata. `scripts/generate_bundled_notices.py --write` regenerates
`BUNDLED-DEPENDENCY-NOTICES.txt` and its machine-readable inventory,
`scripts/bundled_notices_inventory.json`, from the integrity-verified tarballs,
the source map in the same Mermaid tarball, and the upstream lockfiles at the published commits;
`--check`, its default, regenerates in memory and requires a byte-for-byte
match. Both modes need the network. A component whose licence cannot be
established from an exact artifact is listed as a gap, never guessed.
`THIRD-PARTY-NOTICES.txt` and any other locked notice file
are regenerated and reviewed before `--init-lock` re-hashes them into the
`auxiliary` manifest; the CDN render golden is regenerated and the
offline-versus-CDN parity proofs are re-reviewed in the same change. The browser parity proofs compare each offline render with
a checked-in golden of the default CDN build's render of the same fixture
(`tests/fixtures/offline_parity/cdn_render_golden.json`, which records its
generator, date, browser, versions, and CDN requests): the Mermaid SVG
structure, the highlight.js token sequences, and the KaTeX MathML must be
equal. `python tests/regenerate_offline_cdn_golden.py` regenerates it with
network access (`--check` compares instead), and a test fails while its
versions differ from the lock.

The current viewer surface (binding for rebuilds): the root index leads with the
authored bundle title and purpose, compact intro/stats, and one browse inventory.
The deterministic SVG graph preview is secondary, behind a native disclosure;
remaining authored overview content stays available without duplicating inventory.
Generated cards are the sole complete browse inventory while authored
Markdown remains canonical and marker-safe. Every concept has one primary card
with type, link, and tag identity. Recommended ordering is the default; recently
changed ordering, type filtering, clear, no-match, and empty states are
progressive enhancements over complete no-JavaScript browsing.
Concept pages carry a type-tinted header band (icon, reading time, entity chips,
typed relation chips), compact advisory metadata with advanced provenance,
lifecycle, sources, and metadata-only computation behind a native disclosure,
labelled Document details groups with ordered headings after the authored body
(the Document details disclosure starts collapsed at every width, and only the
reader opens or closes it; Studio never does),
a rendered body that fills the article's single content column, connection
recommendations for outgoing/incoming
links (with accessible authoritative comment-count links), and one near-header
`On this page` outline, initially collapsed. SSR emits every native heading anchor
and keeps the outline slot present even on genuinely headingless bodies as a
hidden empty shell, so a supported live mutation fills that same slot without a
reload. The shared `document-outline.js` enhancement owns filtering, one-stop
roving traversal, current-section state, and body-patch observer replacement in
live, static, SPA, and single-file-capable concept surfaces; on graph-detail and
single-file concept selection it rebinds the same owner to whichever document is
selected (targets, current state, and observer renew together, and a headingless
selection re-hides the slot). On serve concept pages the sticky view controls carry
one collapsed `Sections` control driven by that same outline state: it names the
current section, stays keyboard-reachable at every scroll depth, and its popover
list activates sections whose headings land outside the sticky chrome; Escape
closes the popover and returns focus to the control. Heading navigation clears the measured
wrapped sticky shell, focuses its target without a second implicit scroll, and
preserves hash/Back behavior. Resize and BFCache restoration renew geometry
observers without taking ownership of the reader's disclosure/filter state.
Related concepts remain content and
agent proposal actions remain in Studio. On-this-page and Document details stay
in normal flow below tablet width; the sticky chrome is the global document
topbar plus the concept document view controls (Rendered/Source/Split,
Wrap lines, Focus view), which pin directly beneath the measured topbar —
and at the very top in Reading Focus, which hides that shell — so those
controls stay visible and operable at every scroll position. The exception is
a short narrow viewport (at most 640 px wide and at most 480 px tall, for
example 400% zoom of 1280×1024), where that stack would fill most of the
screen: there the topbar and view controls scroll with the page. Skip to
content and section jumps land their target below whatever chrome is pinned.
On mobile the `On this page` disclosure offsets below that same measured stack.
SSR gives potentially wide code, diagram,
figure, and media blocks readable structural wrappers and contextual metadata;
`renderers.js` adds `role=region`, an accessible name, and `tabindex=0` only when
runtime geometry actually overflows, and re-evaluates after resize, live patch,
and progressive rendering. The graph view uses fCoSE with per-type node icons.
Search query, backend mode, type/tag filters, mode-specific filters, and result
limit are URL-owned and survive reload/back navigation. Visible mode labels map
to stable backend values: Best overall (`hybrid`), Exact wording (`lexical`),
Similar meaning (`semantic`), Tagged with (`tag`), Mentions entity (`entity`),
and Connected by relation (`relation`). Submission omits empty/inactive filters
without deleting draft control values. Each default result carries
title, type, path, a readable bounded excerpt when exact evidence exists, evidence
summary, Open action, and Copy passage action only when an exact passage exists.
Type and location share one muted meta line: the type chip and concept id in live search, `type · path` in portable search. An advisory group joins that same line
only when a state differs from its OKF default, in this order and wording:
`Trust: machine-confirmed` or `Trust: human-reviewed` (`unverified` is hidden);
`Lifecycle: draft` or `Lifecycle: deprecated`, any other string as `Lifecycle: `
plus its first 40 code points (`…` when longer) and `(unrecognized)`, a non-string
(or a status longer than 1,024 code points, which the metadata projection
already truncates) as `Lifecycle: invalid value` (`stable` is hidden); and `Stale` when now is on or
after `stale_after`, or `Freshness unknown (invalid expiry)` when `stale_after` is
present but not a valid offset datetime (absent or future expiry is hidden).
Results with only default states, and legacy inventories without metadata, show
no group. Lifecycle and freshness tokens are named in words and set heavier,
never distinguished by colour alone; the group stays outside the result heading
and wraps without horizontal scrolling on narrow screens. Freshness is evaluated
once when results render: live `/__search` uses the server clock, and static,
SPA, and single-file search use the browser clock with strict instant parsing
(the canonical offset form only, never `Date.parse`, impossible dates such as
30 February rejected). An open results page does not re-evaluate staleness;
the concept page's freshness badge keeps its live clock.
Complete source passage, citations, ranking reasons, and advanced actions are
one native collapsed Evidence and context disclosure away. Graph-only results
never claim passage evidence. Search offers a separate **Open matching section**
link only when the attached exact passage has a textual query hit and a
source-owned, uniquely rendered heading target. Ordinary **Open** stays at the
document root. Preambles, headingless or ambiguous passages, and unavailable
proof do not acquire guessed slugs or line-citation anchors. Live, static,
clean-URL SPA, and single-file viewers load the selected document before
admitting the target, focus the actual heading below measured sticky chrome,
and restore search/list/query/mode/filter state with one browser Back step.
Missing, stale, duplicate, unsafe, or plugin-modified targets retain root reading;
ordinary document/source links, Review in Studio, and Sections remain independent.

Section navigation is optional presentation metadata, not a change to strict
schema-v1 passages, corpus manifests, search JSON, or Context Packs. Exported
search and single-file shells carry canonical compact ASCII JSON in
`#okf-section-bootstrap`: `{schema:2, bundle_revision, fragments}`, where each
passage-id entry is exactly `{concept, fragment, heading, source_revision}`.
The map binds to the admitted corpus revision, is capped at 20,000 entries and
2 MiB, and bounds fragment/heading strings at 512/256 code points. Missing,
null, malformed, noncanonical, duplicate-key, or over-cap metadata disables the
optional action without invalidating an otherwise valid old corpus. Each
`source_revision` is `concept_content_rev` of the normalized concept source,
not the corpus revision or a claim about original disk bytes.

Multi-page links carry `okf-section`, `okf-rev`, `okf-c`, and `okf-h` query
intent keys exactly once, not an immediately consumable fragment. The target
page requires its canonical `#okf-rendered-outline` proof
`{schema:1, concept, source_revision, headings:{id:label}}` (at most 1,000
headings / 128 KiB), then checks a unique actual heading inside the displayed
body. The 16-hex FNV-1a label binding detects accidental drift; it is never
authorization. Successful admission replaces that entry with the real hash,
without adding history. Single-file uses its selected-concept owner and
optional `#okf-single-source-revisions` JSON `{schema:1,revisions:{id:revision}}`
(capped at 20,000 entries / 2 MiB), plus the same DOM/label checks. Its search
panel owns the prior history snapshot. An absent destination proof always
retains root reading. Copy vocabulary is Copy passage, Copy concept
context, Build context pack, and Copy selection with citation; every copy
surface states source, scope, and estimated size and provides announced success,
failure, and an exact selectable manual fallback without weakening existing
deadline, generation, revision, or fingerprint fences. Copied concept context
and Context Packs are cited passages for reuse, not a summary, and copying
sends nothing to the agent; the concept page's context action is described
that way, and the command palette's context items say the same. Prepared Context Packs
show selected exact cited passages in a labelled keyboard-scrollable preview;
budget/neighbors are advanced controls. Live/static/single-file preview and copy
state invalidate together when owning inputs, URL, or revision change. No-JavaScript targets
advertise only the retrieval and canonical-context capabilities they can run.
Its selected detail is reading-first: identity and Open action, connection
summary, authored body, relationships/why connected, then Lens explanation and
Graph settings. Each lens answers one
question with a real algorithm (see
[Why the graph is organised into question-named lenses](/explanation/live_studio_design.md#why-the-graph-is-organised-into-question-named-lenses)
for the evidence base): Map (neutral nodes, deterministic Louvain community
grouping and PageRank as size), Themes (strong community grouping, muted
palette-derived community colours and a ranked theme digest), Flow (dagre
layered layout over directed/typed edges, falling
back to directed breadthfirst), Bridges (betweenness centrality as
colour/size + ranked bridgers), Recent (effective generation-time recency
ramp), and Explore neighborhood (topology isolation with a depth control;
the `focus` URL alias remains accepted for compatibility). Every
lens renders a clickable ranked summary with a one-line verdict in the
detail pane. `Selected` names the current graph concept; `Focus view` is
reserved for distraction-free document reading. A `Find in graph` region
combines query and filters; zoom, fit, fullscreen, and reset form one named
viewport toolbar. A `Graph settings` disclosure holds spacing and layout
controls, relationship bundles, the type filter, a relationship-label toggle,
and a 'Colour by theme' toggle, which applies the same muted community ramp
as the Themes lens. Map, Flow and Explore neighborhood default to neutral round
nodes; selection uses a 4px accent stroke. Colour encodings for communities, recency,
bridges, importance or groups use a muted ramp derived from the current palette
and repaint on every palette change. Halos and count badges are square;
content types are never colour-coded. The selected concept's authored body uses the same progressive renderers
(mermaid/KaTeX/highlight.js) as the wiki pages. Suggested starting points rank
structural hubs before the theme digest as ruled rows with mono figures and
2px score bars, never filled importance bars. Hover
previews, shift-click path tracing, keyboard navigation, clickable
legend type filters, deterministic layouts, and live SSE canvas updates
are part of the contract. Concept and relationship alternatives are one-stop
listbox composites: Arrow/Home/End move the active descendant, Space selects,
Shift+Space traces a path, and Enter opens. One polite graph announcer
coalesces lens, selection, match count, neighborhood/path, and error updates.
The optional 45-second tour never opens automatically; dismissal is
bundle/schema scoped and storage denial leaves the graph usable.
On compact screens the Concepts list is the starting pane; explicit selection
opens Details, and the canvas remains optional. Fit uses actual canvas geometry
and only intersecting canvas-owned overlays, not controls in other panes.
Resize follows available space while preserving manually panned view ownership.
Initial relationship-list setup does not scroll the reader to below-body content.
Appearance is one native radio group with Bone (default, light), Slate, Clay,
Plum (dark) and System (Bone for OS light, Slate for OS dark). Each option has a
label, description and four-colour swatch; a 2px accent bar marks the choice.
Arrow/Home/End select, Tab leaves the group, and Escape closes the topmost
Appearance overlay and restores trigger focus. Contrast, border and density
modifiers are removed. Saved choice > configured choice > Bone; legacy names
migrate once to `okf-loom.appearance`. `theme.js` is the single runtime writer,
resolves before first paint, and signals `okf-theme-ready` only after success.
Storage denial retains in-memory switching; cross-tab and live OS changes work.
Deduplicated `okf-loom:themeChanged` events on window carry the current and
previous palette and scheme, including dark-to-dark palette changes.
Public Sans and Azeret Mono normal/italic faces and full OFL notices are
self-hosted on every output. Live/static/SPA pages use relative WOFF2 files;
static/SPA builds (including CDN mode) emit `__static/ledger-public-sans/`
and `__static/ledger-azeret-mono/` with the faces and OFL `LICENSE` files.
Publish both directories with the site. Single-file output alone embeds the
faces as base64 data URLs. All targets use `font-display: swap`; only serve
and SPA preload the upright faces, not file-capable static pages.
The graph key and its counted type/relationship filter controls are owned by
Graph settings (not a separate canvas legend). Directed edges
retain a static dash and arrow cue and may animate dash offset while hovered,
focused, or selected; reduced motion and capture readiness freeze that motion
without changing geometry. The static/no-JavaScript graph representation
consumes the exact meaningful deterministic SVG bytes produced by the public
dependency-free `render.graph_export_svg` helper from canonical graph data.
The index hero derives only its nested link copy from those bytes, marking that
copy decorative so the link's concise computed name is not polluted by SVG
labels; the canonical export remains meaningful everywhere else.
`serve --tunnel` starts a Cloudflare quick tunnel, prints the public
URL, and points cloudflared at a dedicated loopback ingress listener rather than
the local/admin listener. Listener identity and a generation-bound server-owned
capability classify public ingress; client-controlled `Host`, `CF-*`, and
`X-Forwarded-*` headers never do. Public tunnel requests are read-only by
default: every unsafe method rejects them, served public HTML omits all mutation
tokens and advertises `edit: false`, while loopback studio editing remains
unchanged. The explicit `--allow-tunnel-edit` operator opt-in gives every URL
holder a separate public mutation token accepted only by the comment/content
routes (`/__comment`, `/__comment-update`, `/__comment-reanchor`, `/__apply`, and
`/__undo`). It never grants `/__tunnel`, proposal-review, preview, presence, or
other admin/session-control authority and therefore never exposes the local
admin token. The opt-in emits a prominent warning and is rejected when CLI flags
or Studio configuration disable editing.
Public pages mount no unavailable presence/session controls and issue no presence
GETs or POSTs. Local read-only pages may observe presence but never publish it.
Read-only authority and live-update connection status are distinct; inline help
explains read/search/explore/copy without depending on a bundle documentation path.
`scripts/okf-loom tunnel <bundle>` attaches the same read-only-by-default quick
tunnel to an ALREADY RUNNING session (no restart, no lost token/undo history)
by driving the token-guarded `POST /__tunnel` admin route; its own
`--allow-tunnel-edit` provides the same dangerous opt-in. `--stop` detaches it
and revokes/closes its public ingress generation and scoped token; `--status`
reports the current URL and capability. Changing an attached tunnel's public
edit capability requires stop then start, which rotates both ingress generation
and token. The tunnel process always belongs to the serve process and dies with
it.

`--allow-tunnel-comment` is a separate, mutually exclusive, start-only opt-in
on `serve --tunnel` and `tunnel`. It grants only `POST /__comment` (user-authored
create/reply), using a distinct generation-scoped comment token, never the edit
or local/admin token. It rejects `--no-edit` and disabled Studio editing.
Bootstrap reports `comment:true`, `edit:false`, `presence:false`, and
`authority_mode:"public-comment"`. Read-only reports `comment:false`; local/edit
sessions report their effective comment capability independently of content edit.
All update/archive/dismiss/reopen/body-edit/re-anchor, claim/agent, presence,
apply/undo, preview, proposal and administration powers remain denied to shared
commenters. Replies retain the existing auto-unarchive behavior so an answer can
become fresh work; this is the only implicit parent lifecycle change granted.
There are no authenticated reader identities, ownership claims, or “edit own”
permissions. Stop/start rotates authority; copied local credentials or forwarded
headers cannot promote public requests. Denied public requests record a bounded
private `public-denials.json` audit (last 256 denials, route class/method/time,
never headers, URL queries, tokens, bodies or quotes). Audit unavailability never
turns denial into permission. The attach API uses `allow_public_comment:true`;
status/start responses and `server.json` advertise `public_comment:true` and
`tunnel_public_comment:true` respectively only while that capability is active.

Active viewer code has a two-layer gate.
The bundle must declare `viewer.allow_active_code: true`, and the operator must also consent with `--allow-active-code` or a truthy `OKF_LOOM_ALLOW_ACTIVE_CODE`.
Default is off.

## 10. Live collaborative studio model

`scripts/okf-loom serve <bundle>` opens the live collaborative studio by default.
The studio is a local single-user admin tool.
The user directs; the external agent authors.

Core posture:

- The bundle on disk is the source of truth and the bus.
- The browser is a rendered/source/split reading and directing surface.
- Split is a simultaneous two-pane surface admitted by measurement, not viewport alone: the 900px floor must match and the measured workspace must give each pane at least 300px (607px with the divider). Otherwise a split deep link or a transition falls back to Rendered while preserving source content and the saved split ratio. An automatically paused Split is announced and returns when the room does; an explicit Rendered or Source choice ends that. While Split cannot fit, its button stays operable, is marked `aria-disabled`, and says why.
- The page maximum is 1280px; the normal reading column is 760px. Prose is Public Sans at 16px/1.72 capped at 55ch, while ids, paths, timestamps and figures use Azeret Mono with tabular numerals. Code, tables and figures may use the column width and scroll locally. Reading Focus removes global and secondary rails and expands the workspace to the full available width with modest gutters, but keeps the 55ch prose measure, including in Split.
- Space beside the article is allocated by what remains, with a 600px article floor. Related moves below the article before the article would drop under the floor. The Studio drawer defaults to 320px, with a 1px left edge and no shadow; proposal review may use a wider measured width. Comments is a side-by-side rail only if the article keeps that floor after Related has yielded; otherwise it opens as a modal overlay. Non-document pages also use the 1280px maximum.
- Source has a persisted "Wrap lines" control. Unwrapped Source, wide tables and code blocks scroll locally inside labelled, keyboard-focusable regions; the document itself never scrolls sideways.
- The user does not directly edit Markdown in the browser by default.
- The user's studio writes are comments and asks.
- A comment asks for work or discussion. It is saved on the bundle for readers and its agent, which picks it up through its comment loop (`wait`, `comment-claim`, `comment-resolve`); Studio does not start an agent. The composer, headed "New comment", says so under its target line, and its precision line says whether the draft is an exact annotation ("Exact annotation: pinned to this passage and, once sent, highlighted on the page. It is re-checked when the page changes.") or a page comment ("Page comment: about the whole page. No text is highlighted."). An exact draft made in Source view is not highlighted until it is sent.
- The built-in Comments, Changes and Agent activity panels each show one sentence under the panel title saying what question the panel answers, and the panel is described by it only while it shows. Agent activity's sentence names reported status changes only where the session can read presence. Studio home, Proposals (which has its own intro) and extension panels have none; `register()` is unchanged.
- Sharing the Studio through a public tunnel (the server's `tunnel_attach` and `tunnel_detach` activity, origin `tunnel`) writes nothing to the bundle. Changes does not list it, and it adds neither to the unread-changes count nor to Agent activity's recent writes; Agent activity lists the latest five under Sharing, and a toast about sharing alone opens Agent activity. The server records it with actor `cli`, because the share command cannot tell a person from an agent, and the availability line never counts it as an agent action, including sharing rows that older sessions recorded with actor `agent`.
- Studio toasts with a success, warning or error tone show an `OK`, `?` or `!` icon beside a 2px status bar, rather than conveying tone by colour alone. The icon is decorative; the message also communicates the outcome.
- Studio home's status line reports only the live-updates connection ("Live updates: connected", "connecting…", "reconnecting…" or "offline"). Permission is stated under Authority and sharing, and the launcher's idle summary names it (Editable, Comment-only or Read-only); while not connected, a session that cannot edit keeps its word before the connection state ("Comment-only · Offline", "Read-only · Reconnecting"); "Live" is only the connection chip's word for connected. Studio home's quick actions each start an ordinary comment with a suggested request; nothing edits Markdown until an agent picks it up. While the composer's post is pending, being confirmed, or unconfirmed with its sent draft, a quick action is refused with the message a new selection gets and never replaces the draft.
- A text-selection comment action does not steal focus. Escape dismisses one comment state at a time: it first removes the selection anchor while preserving the draft, and the target it is saved to, in the concept composer, in every layout. Then, in the modal overlay, it closes the composer while retaining the draft and restoring focus to the originating content. In the desktop rail Escape never closes the composer: with no selection anchor left it stays native in the text box.
- The server embeds no LLM and runs no agent reasoning.
- The external agent watches comments/events and writes through CLI mutators or whitelisted apply paths.
- There is no per-write approval gate by default.
- Transparency, change list, validation, atomic writes, and undo are the safety net.
- Studio enforces configured `forbidden_paths` and grouped
  `max_edits_per_action` limits on its write funnel. These are not a general
  policy engine or mandatory proposal review; see the exact coverage and limits
  in [configuration constraints](/reference/config_yaml.md#constraints).

The canonical bring-up loop is:

```bash
scripts/okf-loom serve path/to/bundle --no-open &
until scripts/okf-loom token path/to/bundle >/dev/null 2>&1; do sleep 0.2; done
scripts/okf-loom wait path/to/bundle --for comment
```

The agent then claims the comment, writes via mutators, resolves the comment, and waits again.
After starting a background server, always wait for token readiness before the first mutating POST or studio-aware CLI mutator.

See [/tutorials/live_studio_basics.md](/tutorials/live_studio_basics.md), [/tutorials/author_with_agent.md](/tutorials/author_with_agent.md), and [/explanation/live_studio_design.md](/explanation/live_studio_design.md).

## 11. Live protocols and event schema

The live server is stdlib HTTP plus REST and Server-Sent Events.
There are no WebSockets.

Unsafe HTTP requests undergo side-effect-free listener/method/path,
body-independent authorization/header, route-availability, and effective body
cap admission before any body bytes are awaited. Refused or declared-oversized
requests retain their route status/envelope and close without draining the
body. Only a complete admitted body crosses the application mutation fence.
The transport owns one 5-second monotonic body deadline: expiry is typed `408
request_body_timeout`; EOF before the declared `Content-Length` is typed `400
incomplete_request_body`. Both close the connection and leave state unchanged.
Chunked transfer remains unsupported. Route-specific caps (65,536 bytes for
re-anchor and non-create Proposal Review mutations) override the 1 MiB fallback.
See [HTTP routes](/reference/http_routes.md#post-routes) for the normative
outcome table.

Key routes:

| Route | Purpose |
|---|---|
| `GET /__search` | Read-only six-mode HTML search or compatibility JSON array over one bundle snapshot. |
| `GET /__search-form` | Native-form normalization and 303 redirect to strict search; no retrieval or mutation. |
| `GET /__context` | Read-only deterministic schema-v1 context pack as JSON or Markdown over one bundle snapshot. |
| `GET /__data/graph.json[?view=structure]` | Read-only graph projection; `view=structure` omits `bodies` and `documents` only. Strong final-byte ETag, `Cache-Control: no-cache`, and `If-None-Match`/304 revalidation; metadata freshness and comment counts participate in the validator independently of bundle revision. The projection is identical across local and public read-only ingress; no authority is encoded. |
| `GET /__data/doc?id=<concept_id>` | One concept's `{rev, html, raw, frontmatter, backlinks, outgoing, headings}` for in-place patching. |
| `GET /__data/page?document=<path.md>` | Schema-1 canonical Markdown page read: document, nullable concept, document_content_rev, raw, html, page anchor; no guessed projection. |
| `GET /__data/events?since=&limit=&actor=&concept=&order=` | Filtered change-list read over `events.jsonl`. |
| `GET /__events` | SSE stream of live frames. |
| `GET /__comments` | Canonical comment state from `directives.jsonl`. |
| `GET /__diff` | Token-protected diff between saved revisions. |
| `GET /<path>.<media ext>` | Bundle-local media file (image/video/PDF allowlist). |
| `POST /__comment` | User comment or threaded reply. |
| `POST /__comment-update` | User-facing lifecycle/archive transition, or a body edit of a not-yet-resolved comment. |
| `POST /__comment-reanchor` | Target Annotations v2 explicit anchor replacement with row/content revision concurrency and required idempotency. |
| `GET /__presence` | No-store v2 multi-session presence snapshot and signed page-session capability. |
| `POST /__presence` | Legacy agent update, or capability-bound browser upsert/leave with monotonic `client_seq`. |
| `POST /__apply` | Whitelisted update operation. |
| `POST /__undo` | Single or group undo. |
| `POST /__preview` | Render capped in-flight Markdown. |
| `POST /__tunnel` | Server admin: attach/detach/status a quick tunnel at runtime (`action: start\|stop\|status`; start-only `allow_public_edit: boolean`, default `false`). Exempt from `--no-edit` for read-only sharing, but public-edit start is rejected for a no-edit studio; never exempt from the token + Origin/Host guard. |

Bundle-local media serving is contract-bound: only an allowlisted media
extension routes to the file handler; the file must be reachable by the §5
scan (the same exclude/include/gitignore semantics as concept discovery)
and its resolved path must stay inside the bundle root. Responses are
sandboxed (bundle media is user content) and carry an ETag for
revalidation. Static builds copy the same §5-visible media set so exports
render identically to the live studio.

Event rows are JSON objects with server-stamped `id`, `ts`, `seq`, and `rev` when persisted.
Common frame types include `ready`, `changed`, `created`, `removed`, `graph`, `presence`, `comment`, `comment_link`, `activity`, `ping`, and `resync`.

`comment_link` is current behavior.
When resolving a comment with activity ids, the studio appends a back-link event carrying `activity_id`, `comment_id`, and `concept` so the change list can show which comment produced a change.

Polling fallback and replay ordering are part of the contract.
`/__data/events` defaults to newest-first for the change list.
When a client supplies `since`, replay must use `order=asc` so missed events are consumed oldest-to-newest after the cursor.
Newest-first reads are for initial history and activity panels, not cursor replay.

See [/reference/http_routes.md](/reference/http_routes.md).

## 12. Comment lifecycle and agent loop

Comments are directives stored in `<bundle>/.okf-loom/session/directives.jsonl`.
They drive the user-directs / agent-authors loop.

The lifecycle state is independent from archive visibility:

| Field | Values |
|---|---|
| `state` | `open`, `claimed`, `resolved`, `dismissed` |
| `archived` | `true`, `false` |

State ownership:

- Users create comments with `POST /__comment`.
- Agents claim comments with `scripts/okf-loom comment-claim`.
- Agents reply mid-thread with `scripts/okf-loom comment-reply` — the
  clarifying-question channel. The reply is threaded under the root,
  posted with `state=resolved` (a statement, not an ask) so `wait` never
  returns the agent its own reply, and the parent's lifecycle state is
  untouched: reply-without-resolve is the point.
- Agents resolve comments with `scripts/okf-loom comment-resolve`.
- Users dismiss or reopen comments with `/__comment-update`, and may edit
  the body of a not-yet-resolved comment (`/__comment-update` with
  `body`); the edit bumps `updated_at` (so a waiting agent re-receives
  the corrected ask) and re-derives `request_summary`.
- Archive is a soft hide and never replaces lifecycle state.

Current comment records include `id`, `ts`, `updated_at`, `concept`, `anchor`, `actor`, `body`, `state`, `claimed_by`, `claimed_at`, `resolved_activity`, `reply`, `summary`, `request_summary`, `detail`, `archived`, and optional `parent_id`.
New document-target records also carry `document`, a canonical bundle-relative
Markdown path including `.md` (for example `index.md`, `reference/index.md`, or
`log.md`). `POST /__comment` accepts exactly one of `concept` or `document`.
Document page feedback uses `anchor:{"kind":"page"}` or omits the anchor; exact
v2 anchors remain available only for real concepts. A reserved-document row has
`concept:""`, not a fake concept id. New concept document targets retain their
real concept id. Legacy concept rows are never rewritten merely on read.
`GET /__comments?document=path.md` selects both new rows and legacy rows whose
concept corresponds to that path; it cannot be combined with `concept`.
Replies must address the parent's document, inherit its persisted identity, and
follow existing queue/claim/resolve/agent-reply semantics. Document appears in
durable notifications and CLI `wait` output. Page feedback has no text-resolution
claim and remains readable after document removal. Exact concept rows retain
their existing canonical-source availability checks. Page rows cannot re-anchor.
Create still validates the visible, contained current file under the canonical
path lock before the existing WAL/row/store locks; finalized replay precedes
resource lookup. Missing new targets return 404, known unavailable targets 503,
and invalid/ambiguous targets 400. No storage rewrite/migration is needed, but
older runtimes do not understand empty-concept reserved rows: do not downgrade a
session containing page feedback. Preserve its state and use the updated runtime.

Live bootstrap carries `document` for actual concept/index/log files (null for
non-document shells or synthetic listings). Generated regions, titles and other
unmapped content use that owning document, not invented offsets or graph nodes.
`/<dir>/log.md` and `/log.md` are bounded reading routes. The page read route
accepts only one `document` parameter, rejects aliases/traversal, applies scanner
visibility and descriptor-relative no-follow containment, and uses the existing
16 MiB canonical-read cap. Its SHA-256 `document_content_rev` hashes exact UTF-8
source bytes. Reserved pages deliberately have no annotation projection.

`ts` is immutable creation time.
`updated_at` bumps on claim, resolve, dismiss, reopen, archive, unarchive, reply, and summary updates.
`claimed_at` is set on claim acquisition/owner change, preserved across unrelated
claimed-row updates, and cleared when leaving `claimed`; malformed retained
values are exposed through quarantine rather than silently extending a claim.
`summary` is the agent-authored done/status blurb; `request_summary` is the ask summary.

A page-level comment's public anchor is exactly `{"kind": "concept"}`; the target
is the request's top-level `concept`, never a field inside the anchor. Browser
replies use the same anchor with `parent_id`. Records created without an anchor
(including `comment-reply`) store `{}`. Readers treat `{"kind": "concept"}`, `{}`,
and legacy `{"kind": "concept", "ref": <same concept>}` or
`{"kind": "source", "ref": <text>}` rows as page-level: they are never projected
as a highlight or offered re-anchoring, and a concept anchor naming a different
concept is rejected. Studio maps a Source-view selection to a `text-quote`
anchor only when the pane is provably a byte-exact suffix view of the canonical
source and the span lies inside exactly one block. Otherwise, and for every
graph-preview selection (that renderer has no canonical source projection), it
sends a page-level comment and keeps the selection as a quoted selection in
`detail.selection`: `surface` (`source`, `graph`, `rendered` or `page`), `precision` (`page`), `quote`
(at most 1,024 code points), `truncated`, and `reason`. That quoted selection is shown to
the reader and delivered to the agent by `wait`; it is never an anchor, an
offset, or evidence of where the text lives. The server stores `detail`
opaquely, so `surface` is advisory too.

Every selection therefore ends in one of two honest outcomes. Rendered text
inside one mapped block (paragraphs, list items, fenced code, one table row's
cell) stores an exact v2 anchor. A table is mapped per row: a selection inside
one cell is exact, including through an escaped pipe (the two source code points
`\|` are the one visible `|`, the only backslash transformation the browser
accepts), while a selection through two cells or two rows is not one source
passage. Boundaries resting on the empty edge of a neighbouring text node are
trimmed to the text actually selected before the block is chosen. A rendered
selection that crosses blocks, cells or rows, or lies in an otherwise
unmappable block, uses surface `rendered`; a selection in the title, metadata,
generated regions, or anywhere on an index or log page uses surface `page`. In
both cases the composer states, before sending, that this is a page comment and
why, and that the selected text is quoted in the comment, not attached to exact
text. Content with no selectable text, such as an image, offers no selection
action; the page comment control remains. On the wire a page-level comment is
`{"kind":"concept"}` for a concept and `{"kind":"page"}` for a reserved
document.

Studio separates projection *admission* from *exact anchoring*. Loading a page
and its comments requires only that the projection metadata is coherent with the
document revision. Exact anchoring additionally requires the strict client-side
verification of the source map and fails closed: when that cannot be completed,
the composer says "Page comments only", selections become page comments with
the selected text quoted in the comment, and the comments list still loads. A
projection for another revision
is still rejected.

Each document surface (concept, root and directory index, log, and the concept
selected in the graph) has a page feedback control showing its saved comment
count; optimistic rows that are still posting are never counted and offer no
actions. The comments list is scoped to the current document by default, says
how many threads are elsewhere, and can show all pages, where each root names
and links its target. A surface with no document (the graph with nothing
selected, or a synthesized root listing) says so, keeps the draft, disables Send
and posts nothing.

On the graph the comment target is what the detail pane actually shows: the
selected concept's preview, or a reserved index or log document shown there. A
cleared selection is no target, even though the hidden preview keeps its last
concept id. A graph draft is *pinned* once it has a saved target and something
to keep there: typed text, selected-text context, or a post that is pending,
uncertain, or accepted but not yet confirmed. A pinned draft keeps its target
when the selection changes. Only a deliberate action changes it: Move, Cancel, a
confirmed post, or explicitly commenting on a selection in another concept B
("Comment on the selected text" in B's preview), which retargets the draft to B
with its text and the new selected text.
Empty drafts, and typed drafts that have no target yet, follow the selection.
Concept and reserved pages are unaffected: their route owns the target. While a
pinned draft's target A differs from what is shown, the composer shows a
labelled "Draft target" group before the text box. It is not a live region; the
text box's description includes its note.

| Shown | Note | Move |
|---|---|---|
| B or nothing, post still being confirmed | "This comment is still being confirmed on A, so it can't be moved." | none |
| Nothing | "Your unsent comment stays on A. No concept is selected." | none |
| B, draft has selected text | "Your unsent comment and its selected text stay on A. Press Esc to remove the selection before moving it." | none until Escape removes the selected text |
| Another concept B | "Your unsent comment stays on A." | "Move draft to B" |

The first row that applies gives the note: a post still being confirmed, then
nothing selected, then selected text. Escape in the composer removes the
selected text in every layout, so the note is the same in the desktop rail and
the modal overlay. A result that could not be confirmed holds the draft only
while it is still the one that was sent: once its text, selection or target
changes, the note and Move update at once, and restoring the sent draft holds
it again.

Move saves the draft to the shown target and returns focus to the text box; the
composer's live target line then names the new target. Each distinct other selection is announced once
in the shared status region ("Your unsent comment stays on A. It was not moved
to B."); clearing the selection announces nothing, and showing A again resets
the record. When a post is rejected with 404 `target_not_found`, the composer
says "The comment was not posted because A no longer exists. Your text is still
in the composer.", keeps the text and its target, and keeps Send and Enter from
posting to A until Move, Cancel, emptying the draft (it then follows the
selection), or A is shown again. Other routes show the same
message without holding Send.

Every post settles the composer it started from; it never stays on "Posting
comment…". A change of revision on the same page does not orphan it, and
neither does Back or Forward between history entries of the same page (a
fragment, the graph's selected concept), although live.js counts those as
navigation. While a post is pending, selecting text does not retarget it: the
selection is ignored and the studio says "Your comment is still being posted.
Select the text again when it has finished." When the result cannot be
confirmed (network failure, timeout, 429 or 5xx, or a 2xx whose body cannot be
read or does not arrive within the post's deadline), the composer restores the text and its target, and "Retry send" resends
them with the same idempotency key ("Cannot confirm the comment was posted.
Retrying is safe and won’t create a duplicate."). That holds only for the
draft that was sent: once its text, selection or target differs, the button
says "Send", the next post uses a new key, and the status says "Your earlier
comment may already be posted. Sending this changed comment posts it as a new
comment; check the comments first." Restoring the sent draft restores "Retry
send". While the sent draft is held, a new selection is refused ("Your comment
could not be confirmed. Retry it, or edit or cancel the draft, then select the
text again."). A post that was accepted but whose confirming re-read of the
comments failed offers "Retry refresh", which only re-reads them and never
posts again, even when a newer revision reset the draft's selection meanwhile;
until it is confirmed or dismissed, a new selection is refused ("Your comment is
still being confirmed. Use Retry refresh or Dismiss, then select the text
again."). Only a change of revision during that re-read lets the accepted row
already listed confirm the post: after Back or Forward within the page, or on
the graph, a failed re-read still offers "Retry refresh" and never says
"Comment posted.". An exact selection
whose revision the page no longer shows cannot be resent that way: the text comes
back without its selection, the key is dropped, and the composer says "Cannot
confirm the comment was posted, and the page changed, so its text selection was
lost. Check the comments before sending it again." Cancel frees the composer at
once: it is no longer busy. Cancel after a result that could not be confirmed,
or Dismiss after an accepted post whose re-read failed, clears the draft but
keeps that post's key, as an inline reply keeps its key across Cancel: the same
text and target sent again is "Retry send" under that key, and anything else is
sent as a new comment with the "may already be posted" warning. A post that was cancelled, or replaced by a newer
draft, while in flight only adds its confirmed row to the comments list when it
lands; it never clears, moves or overwrites the newer
draft. A successful post clears the composer only if its draft is unchanged. A
result that arrives after navigation changes nothing on the page it left.

An inline reply or edit makes its text box read-only while it is being sent, so
no text typed after sending is lost when the box closes. A reply whose result
cannot be confirmed offers "Retry reply", which resends the same text with the
same key ("Cannot confirm the reply was posted. Retrying the same text is safe
and won’t create a duplicate."). Once the text differs from what was sent, the
button says "Reply" and the status says "Your earlier reply may already be
posted. Sending this changed text posts it as a new reply; check the thread
first." Restoring the sent text restores "Retry reply". A reply interrupted by
Back or Forward within the page settles the same way: unconfirmed, with its
text. An edit save that gets no answer within 15 seconds unlocks its text box,
warns that the edit could not be confirmed, and re-reads the comments.

Agent availability must distinguish a current claim ("an agent has claimed N
comments") from historical action ("an agent acted; whether it is still running
is not known here"). A retained `claimed_by` establishes historical ownership,
not the time of the last agent action: unrelated body/archive changes also bump
`updated_at`. A dated action requires `claimed_at` for the current claim, an
agent-authored reply's creation time, or an explicitly attributed lifecycle or
activity event; sharing through a public tunnel (origin `tunnel`) is never one,
whatever actor it carries. Named acting labels need not equal the literal `agent`.
Legacy comment events without a lifecycle discriminator describe row authorship,
not who claimed/resolved the thread; do not invent missing attribution. The presence slot is not
evidence: any token holder, including the browser's Watching toggle, can set it,
so a non-idle mark with no such activity is reported as a status flag, not proof
that an agent is running, and the toggle's copy says it starts nothing. The
presence chip is described by "Reported status, not proof an agent is
running.", so that caveat does not depend on its hover title. With no
evidence the Studio says no agent has acted and that comments wait for an
external agent, and shows how to connect one. Sessions that cannot read presence
(comment-only sharing) show no availability claim at all.

Threading is capped at two displayed levels: parent, reply, reply-to-reply.
Deeper nesting is hoisted to root.
Archive is root-only and server-enforced: every comment in the thread must be `resolved`, otherwise archive fails.
Replying to an archived thread auto-unarchives the parent.
Compatibility records with `state == "archived"` are normalised on read to `archived=true, state="open"`.

Canonical foreground agent loop:

```bash
scripts/okf-loom wait path/to/bundle --for comment
scripts/okf-loom comment-claim path/to/bundle 01JABED9K0 --summary "adding dependency link"
scripts/okf-loom comment-reply path/to/bundle 01JABED9K0 --body "Did you mean orders or order_items?"  # only when the ask is ambiguous
scripts/okf-loom presence path/to/bundle --state editing --focus tables/orders --message "linking 3 of 7 tables"
scripts/okf-loom link-add --bundle path/to/bundle --source tables/orders --target tables/customers --group-id PASS1
scripts/okf-loom comment-resolve path/to/bundle 01JABED9K0 \
  --activity 01JABED5K7 \
  --reply "Done - linked orders to customers." \
  --summary "done: link added"
```

`scripts/okf-loom wait` is block-once and foreground.
Do not background it; the caller is the consumer.
The returned comment carries a `queue` field — `{pending, ids}` for the
OTHER open comments still waiting — so the agent sees backlog depth
without diffing `comment-list` between loop turns; the one-work-item
contract itself is unchanged.
`scripts/okf-loom watch` is the continuous feed variant for a long-running consumer and can replay from `--since` before tailing live.

See [/reference/comment_lifecycle.md](/reference/comment_lifecycle.md) and [/reference/cli.md](/reference/cli.md).

### 12.1 Annotations v2 contract (normative, implemented)

This subsection is the canonical implemented contract for Annotations v2.
The Python resolver, HTTP and storage boundaries, renderer projection, Studio
UI, and Python/browser tests enforce it. Existing comment lifecycle rules above
remain in force. The roadmap's unsafe first-match replay remains forbidden.

#### Identities, authority, and limits

The Markdown concept is content truth. Comment/anchor intent is the latest
complete full row after merging `directives-checkpoint.json` with retained
`directives.jsonl` generations; after pruning, a checkpoint-only latest row is
still authoritative. Resolution and DOM marks are derived projections, never
stored truth, and there is no database.

| Name | Exact form and owner |
|---|---|
| `concept_id` | Existing canonical bundle-relative concept id. It is obtained from the comment row, not accepted as a re-anchor target. |
| `concept_content_rev` | Lower-case 64-hex SHA-256 of the exact canonical Markdown bytes. It changes on every content-byte change and is a string, never an event sequence. |
| `comment_id` | Existing ULID-like id: 10–26 upper-case Crockford Base32 characters, excluding `I`, `L`, `O`, and `U`. |
| `comment_row_token` | Opaque non-empty ASCII string, at most 128 bytes, generated for each appended full row. It identifies row content/lineage and is not a timestamp. Clients compare it byte-for-byte. |
| `event_cursor` | Globally unique, strictly increasing unsigned decimal event sequence, serialized as a JSON integer while within the safe integer range. It is unrelated to comment ids and content hashes. |
| `anchor.schema` / resolver version | Integer `2`. A resolver behavior change that can change results requires a new version; unsupported versions are invalid. |

All lengths below are Unicode code points unless explicitly stated as bytes.
The re-anchor raw HTTP body is capped at 65,536 bytes **before** JSON parsing.
`exact` is 1–16,384 code points; `prefix` and `suffix` are 0–256 each;
`heading` is 0–512; `block.id` is 1–256 ASCII characters; and the total
serialized anchor must fit within the route body cap. Strings preserve exact
source characters: no NFC/NFKC or other Unicode normalization is performed.

Canonical source, block, and quote offsets are zero-based, half-open Unicode
code-point offsets. `start < end`; booleans are not integers. Line endings are
preserved in source identity, while block extraction's versioned representation
normalizes CRLF to LF. JavaScript DOM `Range` offsets are UTF-16 code units and
must be explicitly converted by counting code points before persistence and
back to UTF-16 before DOM projection. Shared Python/JavaScript parity fixtures
must cover BMP text, astral emoji, combining marks, CRLF/LF, HTML entities and
punctuation, and visible text containing Markdown syntax.

#### Closed v2 anchor schema

Public requests are closed: every object below rejects unknown keys and every
field is required, including empty context strings. Stored comment records,
anchors, `detail`, and future extension fields are open for round-trip storage:
writers must preserve unknown stored fields unchanged. This distinction permits
forward-compatible storage without silently accepting misspelled public input.

```json
{
  "schema": 2,
  "kind": "text-quote",
  "exact": "customer_id",
  "prefix": "column ",
  "suffix": " references",
  "heading": {"path": ["Schema", "Columns"]},
  "block": {
    "id": "paragraph:17",
    "hash": "sha256:7a1f000000000000000000000000000000000000000000000000000000000000",
    "start": 120,
    "end": 181
  },
  "position": {"start": 127, "end": 138}
}
```

| Field | Type / cap | Semantics |
|---|---|---|
| `schema` | integer constant `2` | Anchor/resolver schema. |
| `kind` | string constant `text-quote` | No alias or case folding. |
| `exact` | non-empty string, ≤16,384 | Exact selected canonical source text; never truncated. |
| `prefix`, `suffix` | string, ≤256 each | Maximal available bounded context immediately adjacent to `exact` inside the canonical block; shorter only at a block boundary. |
| `heading.path` | array of 0–16 non-empty strings, each ≤512 and total ≤2,048 | Outermost-to-innermost canonical heading text, without Markdown markers. |
| `block.id` | ASCII string matching `^[A-Za-z][A-Za-z0-9._:-]{0,255}$` | Deterministic extractor-v2 block identity; evidence, not permanent identity. |
| `block.hash` | string matching `^sha256:[0-9a-f]{64}$` | SHA-256 of extractor-v2 block text after CRLF→LF only; no whitespace or Unicode normalization. |
| `block.start`, `block.end` | non-negative integers, `start < end` | Block span in canonical Markdown code points. |
| `position.start`, `position.end` | non-negative integers, `start < end` | Selected span in canonical Markdown code points; it must equal `exact`, lie within `block`, and have length `len(exact)`. |

Canonical block IR version `1` is the single block grammar consumed by the
extractor, renderer, and projection builder. It walks canonical Markdown source
in source order and emits headings,
paragraphs, list items, block quotes, fenced/indented code blocks, table rows,
and HTML blocks as containing blocks. A block span includes its exact source
text but excludes a trailing block-separating blank line. `block.id` is
`<kind>:<one-based-source-order>`; its ordinal is evidence only. Capture fails
closed if a browser selection cannot map to exactly one canonical source span.
The IR also preserves list continuation and nested-item
text, removes rendered task-list checkbox controls from canonical-visible text,
preserves indented-code and multiline-HTML line structure, owns footnote output,
and maps escaped table pipes to their exact canonical offsets. It also covers
ordered `1)` lists, lazy block quotes, nested same-name HTML containers,
multiline comments/CDATA/processing instructions, pipe-containing prose versus
tables, thematic breaks, and setext precedence. LF and CRLF produce the same
block grammar and normalized hashes. These are conformance fixes to the already
declared v2 anchor/resolver contract, not a resolver-version change: resolver
version `2` remains candidate-set based and commutative. Projection schema `1`
adds `block_ir_version:1` as backward-compatible metadata. Initial, live, and
static projection consume this same IR rather than reparsing blocks.

#### Candidate-set resolver and result algebra

The resolver first validates the entire anchor and canonical source. It then
extracts all v2 blocks and enumerates the full candidate set `C`: **every**
exact, code-point-aligned occurrence of `exact`, including overlapping
occurrences. It must never return the first match or use fuzzy scores.

Against the unchanged full set `C`, it independently computes one support set
for each evidence item: exact original `position`; stored block span/id;
`block.hash`; `heading.path`; adjacent `prefix`; and adjacent `suffix` (bounded
at current block edges). A support set contains every candidate compatible with
that item. Support-set computation never consumes or mutates another item's
result. Empty support means that context moved or changed and is ignored for
selection, but is reported in `changes`. All non-empty supports are intersected
in one commutative operation; implementations must produce the same result for
every evidence evaluation order.

1. If `C` is empty, return `stale/exact_not_found`.
2. If every support is empty, the effective set is `C`: one candidate is `resolved/unique_global_exact`; multiple are `ambiguous/insufficient_context`.
3. Otherwise intersect every non-empty support. One member is `resolved` (`original_position` when unchanged position and context identify it, otherwise `unique_context`); multiple are `ambiguous/multiple_exact_candidates`.
4. An empty intersection when two or more individually non-empty supports are disjoint is `invalid/conflicting_evidence`, not stale and never a fallback to one support. An empty intersection from a single non-empty support is impossible by definition. Thus no loop order, preferred field, or early return can affect the result.

A single global exact occurrence may resolve despite a moved block, renamed
heading, or changed bounded context because empty supports are ignored and the
changes are reported. Multiple occurrences resolve only when the
order-independent intersection uniquely identifies one.

Resolver output is revision-qualified. `status` is exactly `resolved`, `stale`,
`ambiguous`, or `invalid`. `position` and current `block` occur only for
`resolved`; `candidate_count` occurs for `stale`/`ambiguous`; every result has
`concept_content_rev` and `resolver_version:2`. `changes` is an ordered subset
of `position_moved`, `block_moved`, `block_changed`, `heading_changed`,
`prefix_changed`, and `suffix_changed`.

| Status | Allowed `reason` codes |
|---|---|
| `resolved` | `original_position`, `unique_context`, `unique_global_exact` |
| `stale` | `exact_not_found`, `no_compatible_candidate` |
| `ambiguous` | `multiple_exact_candidates`, `insufficient_context` |
| `invalid` | `malformed_anchor`, `unsupported_schema`, `invalid_offsets`, `oversized_anchor`, `source_mapping_failed`, `conflicting_evidence` |

Invalid, stale, and ambiguous are resolver outcomes, not HTTP failures. Only a
uniquely `resolved` result may create a highlight or enable Jump. Any other
result removes an existing mark.

Legacy `{kind:"text",ref:<non-empty string>}` anchors remain readable and are
never rewritten merely by reading. The compatibility resolver enumerates all
exact `ref` occurrences: one is `resolved/unique_global_exact`, zero is
`stale/exact_not_found`, and more than one is
`ambiguous/multiple_exact_candidates`. Other malformed legacy forms are
`invalid/malformed_anchor`. Public legacy `ref` uses the same 16,384-code-point
bound as v2 `exact`, and occurrence scanning stops once ambiguity is established.
Explicit re-anchor writes the first v2 row.

#### Row ownership and concurrency

Every full-row writer (create, claim, reply, resolve, dismiss/reopen,
archive/unarchive, body edit, summary update, and re-anchor) acquires the same
per-comment cross-process lock. Inside that lock it loads the latest full row,
validates any required `comment_row_token`, merges only fields owned by that
operation, generates a new row token, and atomically appends one complete row.
Unknown stored fields are copied unchanged. Re-anchor owns only `anchor` and
the row metadata; lifecycle/body writers must retain the latest anchor. Token
collision, stale token, lock failure, partial write, or rotation failure fails
closed without an observable partial row. `updated_at` is a human timestamp,
not a concurrency token. Re-anchor is not a Markdown mutation and does not
enter undo history; Markdown undo leaves anchor intent and forces re-resolution.

The global acquisition order is concept lock → idempotency-key lock → comment
lock → directives append/rotation lock. Comment creation first resolves the
parent and therefore the effective concept; every creation verifies that the
effective canonical concept exists. Validation and directive append share the
concept lock. Finalized create/re-anchor replay is reconciled before current-
resource access, so a response-lost retry does not become unavailable merely
because the concept was subsequently deleted.

Before directive generations are pruned, durable
`directives-checkpoint.json` records the latest complete full row for every
comment. Readers merge it with retained generations; checkpoint or prune
failure retains old generations and fails closed. Transaction-bearing rows are
also summarized in bounded `.directive-transaction-results/` records. Prepared
and finalized replay consult those results, not lifecycle copies of transaction
metadata or independently pruned history. Prepared records count toward the hard
WAL cap; retired outcomes become fail-closed tombstones and then a bounded
tombstone filter. Result and notification sidecars retire only after the
corresponding WAL/tombstone retirement is durable.

Re-anchor idempotency is a crash-consistent write-ahead protocol, not a
best-effort response cache. Under the same per-comment lock, the server writes
and fsyncs an endpoint-scoped WAL transaction containing transaction id,
idempotency key, canonical request fingerprint, comment id, expected tokens,
state=`prepared`, and creation/expiry times. The appended directive row carries
that transaction id and fingerprint. After the atomic append/rotation is
durable, the WAL is atomically finalized with resulting row token, status, and
response, then fsynced before response. Replay reconciles a `prepared` WAL
against all retained directive rows under that lock: a matching transaction
row finalizes/replays success; no matching row permits the same validated append
using that transaction; conflicting identity/fingerprint fails closed.

Crash semantics are exact: before WAL, no intent exists and retry starts
normally; after WAL but before append, replay resumes the prepared transaction;
after append but before finalize/response, replay discovers the row and never
appends twice; during rotation, either the atomic old/new directive set exposes
the transaction row or recovery treats rotation as unavailable—never as proof
that append did not occur. Prepared records with no appended row may be removed
only after their expiry plus the maximum directive-rotation recovery window.
Finalized records retain the 24-hour TTL and bounded-file cap. Expired or
cap-evicted records never authorize uncertain automatic replay: if retained
directive history cannot prove the transaction outcome, the endpoint returns
`409 idempotency_conflict` and requires canonical refetch plus a new user intent.

Every comment creation or reply, including a request without an idempotency
key, receives a durable notification/outbox identity. Create and re-anchor
notifications use `.notification-receipts/` records with `pending` and
`committed` states. The immutable, strictly validated full payload receipt is
durable before directive visibility, so normal and recovered payloads—including
new lifecycle row tokens and `parent_id`—match even after later mutation.
Re-anchor uses the same full event shape. WAL retirement reconciles pending delivery
before deleting the WAL, and tombstone replay performs the same durable
check-and-deliver. `pending` may append one missing event; `committed`
suppresses duplicates after event rotation.
Keyless committed identities compact into a bounded durable no-false-negative
checkpoint tied to retained event/directive authority; cap eviction, event
pruning, and restart cannot redeliver an older committed notification. Legacy
checkpoint formats that cannot prove complete membership fail closed rather than
starting with an empty membership filter.
Session storage is pinned first, then `.notification-receipts/` is opened
relative to that trusted directory descriptor. One pinned receipt dirfd remains
owned across enumeration, reads, cleanup, and reconciliation; substitution of
the pathname cannot redirect later consumption. Receipt leaf operations remain
descriptor-relative/no-follow; constrained crash-residue temporaries are
validated and removed during recovery.
Claim acquisition owns `claimed_at`; unrelated mutations preserve it, leaving
claimed clears it, and malformed claimed timestamps are visibly quarantined.

New lifecycle notifications carry `operation:"lifecycle"`. Their `actor` is the
explicit mutating label (claim/resolve's supplied `claimed_by`, or `user` for
admitted HTTP row updates), and their `ts` is the appended row's `updated_at`.
Trusted library updates without actor evidence emit `actor:null`; an inherited
`claimed_by` or original comment author is not mutation attribution. The row's
`actor` and `ts` retain creation authorship/time, including legacy absence.
Existing events are not rewritten. Create/re-anchor receipt shapes, immutable
payload recovery, event cursors, row tokens and lock ordering remain unchanged.

#### State and async lifecycle matrices

| State | Authority / writer | Reset and race rule |
|---|---|---|
| Concept text and `concept_content_rev` | Markdown / derived hash | Any byte change, external edit, undo, route or project switch invalidates resolution. |
| Active concept identity | Router's canonical concept id | A response for another concept is discarded. |
| Comment row and row token | Latest full directive / locked writers | Exact token match required before projection commit. |
| Resolution / DOM mark | Pure resolver / renderer | Keyed by `(concept_id, concept_content_rev, comment_id, comment_row_token, resolver_version)`; never persisted. |
| Selection draft | One active browser editor | Local only; relevant document change clears it and reports conflict. |
| Pending operation/generation | Browser async controller | Monotonic generation plus AbortSignal fences late work. |
| Document publication | Browser studio; bumped when the shown revision changes, including the first document (a same-revision re-commit is not a publication) | A document, comments or live-patch read that started before a newer publication and would show another revision is re-read within its bounded budget or dropped; it never rolls the page back. |
| Anchor health | Resolver | Never changed by transport timeout/failure. Each healthy state is explained on the card: resolved (Attached) adds "Exact annotation: pinned to this passage and highlighted."; a page comment adds "About the whole page; no text is highlighted.", or, with a quoted selection, "Selected in the graph preview/Source view/document — quoted here, not attached to exact text." A reply has no line of its own. |
| Transport status | Browser request state | `idle/checking/selecting/saving/conflict/save_failed`; separate from health. |
| Event cursor | Server event sequence / client cursor | Monotonic globally unique cursor; never a comment id or content hash. |
| External override | Operator-approved viewer plugin | Cannot write canonical anchor state or bypass resolver/security. |

| Scenario | Owner/deadline | Mutation and terminal behavior | Retry / late-result rule |
|---|---|---|---|
| Resolve before dispatch | Browser generation; synchronous validation | None; typed invalid result. | No retry until input changes. |
| Canonical fetch in flight | Browser; 10 s deadline with AbortSignal | None; transport failure leaves health unchanged. | At most 3 attempts for timeout/429/5xx, exponential 250/500 ms plus 0–100 ms jitter. |
| Resolve completes after timeout/new revision | Browser generation/full projection key | None; result discarded. | Fresh canonical fetch/resolve only. |
| Re-anchor timeout before dispatch | Browser validation/deadline owner | No POST and no mutation. | Fix input or retry with same intent/key. |
| Re-anchor request in flight | Browser; 15 s AbortSignal; server lock owns atomic mutation | Zero or one full row. | Retry only timeout/429/503, max 3, 250/500 ms + jitter, same key/body. No retry for other 4xx. |
| Append succeeds; response is lost | Server idempotency record under comment lock | Exactly one row. | Same key/body replays success; canonical refetch decides UI. |
| Create append succeeds; notification fails | Create WAL plus transaction-tagged directive/event rows | Exactly one comment and eventually exactly one notification. | Replay proves or appends the missing event before marking notification delivered. |
| Response arrives after navigation/pagehide | Route/generation owner | Persisted server result remains canonical. | Response cannot alter departed UI; next load refetches. |
| Superseded fetch/backoff or A→B→A navigation | Operation generation plus production navigation epoch | Superseded work aborts synchronously; only the current route may commit. | No retry/backoff or DOM commit by the old owner. |
| Read overtaken by a newer document publication | Document publication counter plus the shown `concept_content_rev` | None; the newer document stays. | Comments re-read within the skew budget (at most two re-reads); a held live patch re-reads once; a selection capture keeps the shown document or aborts. Otherwise the result is dropped. |
| Composer post cancelled or replaced in flight | Composer post key | Cancel frees the composer at once (no longer busy). A late confirmed row only joins the comments list; the newer draft, its target and busy state are untouched. | The abandoned attempt neither retries nor announces. |
| Post or reply settles after a same-page revision change or Back/Forward | Concept plus navigation epoch, not revision; the page is left only when the concept changes, or the navigation epoch and the document generation both move with no newer publication of the page (Back/Forward within the page keeps it) | The composer or reply box always settles as posted, rejected or unconfirmed; success clears the composer only if its draft is unchanged. A 2xx whose body cannot be read, or does not arrive within the post's deadline, is unconfirmed. An accepted post whose confirming re-read fails is confirmed by its listed row only when the revision changed; after Back/Forward, or on the graph, it offers Retry refresh. A reply that Back/Forward aborted, or whose result it fenced out, is unconfirmed, and that result is never listed. | Unconfirmed retries reuse the key while the draft is the one sent, except an exact selection whose revision is gone: its key is dropped and the lost selection is reported. |
| Selection or typing while a post is in flight or unconfirmed | Composer post / inline reply or edit box | A composer post that is pending, unconfirmed with its sent draft, or accepted but not yet confirmed ignores new selections and quick actions and says so; inline reply and edit text is read-only while sending. | An unconfirmed composer post or inline reply retries with the same key only while its draft is the one sent; a changed draft is a new comment or reply, with a warning that the earlier one may already be posted. Cancel, or Dismiss after a failed confirming re-read, keeps the composer post's key for the sent draft, as Cancel does for an inline reply. Retry refresh only re-reads the comments; it never posts. |
| Inline edit save in flight | Edit box; 15 s deadline with AbortSignal | Row-token check: zero or one update. At the deadline the text box unlocks, a warning says the edit could not be confirmed, and the comments are re-read. | No automatic retry; saving again is a new row-token check. |
| SSE/poll reconnect or resync | Event cursor + canonical readers | Events notify only; no anchor mutation. | Replay oldest-first, then canonical comments/doc refetch. |
| Claim/resolve/body edit during editor | Per-comment lock + browser merge | Each operation owns only its fields. | Unrelated row change preserves editor; save uses latest expected token or conflicts. |
| Public API/docs parity | Server boundary | Typed documented status/envelope. | Drift is a contract-test failure. |

#### Projection, SSE, polling, and cache contract

Rendered concept HTML and `/__data/doc` expose the same revision-qualified
`annotation_projection` object. Schema `1` contains `resolver_version:2`,
additive `block_ir_version:1`, the
64-hex `concept_content_rev`, `offset_unit:"unicode-code-point"`, `complete`,
and ordered block records `{block_id,source_start,source_end,heading_path,dom_key}`.
The renderer assigns unguessable ownership capabilities to real projection
nodes before viewer plugins run. After plugins run, exact finalization accepts
only surviving, unique renderer-owned capabilities, strips them, and emits
opaque numeric keys. Other plugin-added markup is ignored unless the admission
rules below refuse it; removed, copied, forged, or text-altered owned nodes make
the map incomplete. Plugin-added elements left open at the end of the body are
ignored only when none of them is or contains a mapped node. Only elements
whose tree construction the finalizer models may be a keyed owner, enclose one,
or sit inside one: the renderer's blocks, lists and tables (blockquote, div,
hr, li, ol, p, pre, section, ul, table, thead, tbody, tr, th, td, h1–h6),
div-like containers (address, article, aside, figcaption, figure, footer,
header, main, nav), definition lists (dl, dt, dd) and plain phrasing (a, abbr,
b, bdi, bdo, br, cite, code, data, del, dfn, em, i, img, input, ins, kbd, mark,
q, s, samp, small, span, strong, sub, sup, time, u, var, wbr). Any other element
there (for example button, select, option, optgroup, form, nobr, ruby, rb, rt,
rtc, rp, or an unknown or custom element) makes the map incomplete. So do: a
heading start inside an open heading; an anchor start inside an open anchor; a
table part anywhere in the document that does not start directly inside its
required parent (caption, colgroup, thead, tbody and tfoot in table; col in
colgroup or table; tr in table, thead, tbody or tfoot; td and th in tr); a line
feed, literal or as a character reference, directly after a pre start inside an
owner; any text other than HTML ASCII whitespace (tab, LF, FF, CR, space),
including resolved character references and U+00A0, directly inside a table,
table section or row; a noscript whose end tag is not the first `</noscript`
followed by whitespace, `/` or `>` after its start tag, for example one hidden
in an attribute value or comment; any comment anywhere in the document that is
not well-formed HTML5 comment syntax (`<!--`, then text that does not start
with `>` or `->`, does not contain `<!--`, `-->` or `--!>` and does not end
with `<!-`, then `-->`), including `<!-->`, `<!--->`, a `--!>` closer and an
unclosed comment; any literal NUL anywhere in the document; and, in owner text,
a literal CR or a numeric character reference whose Python `html.unescape`
value differs from the code point HTML5 emits (the C0 controls other than NUL,
tab, LF, FF and CR, DEL and the noncharacters). A mapped node inside
`noscript`, or
`noscript` inside a mapped node, makes the map incomplete because a scripting
browser parses `noscript` content as unrendered raw text. Mapped
renderer-owned elements carry only the opaque numeric
`data-okf-projection-key`; canonical text is never copied into attributes.
`dom_key:null`, an incomplete map, revision mismatch, or unequal canonical and
visible occurrence counts is an explicit unmappable gap and must fail closed.
Blockquote visible text is derived recursively through the same inner block
grammar as rendering, including headings, thematic breaks, mixed lists, nested
quotes, and CRLF. Any residual owner capability or forged projection-key text in
attributes, comments, malformed constructs, or text forces incomplete output;
entity and percent encodings are iteratively canonicalized under a hard bound
before capability/key classification. Canonicalization reports fixed-point
convergence separately from budget exhaustion; exhaustion forces safe incomplete
fallback. Detected contamination—including malformed or unterminated tag-like
regions—is scrubbed with the same bounded semantics, and returned HTML must be
capability-free rather than merely downgrading `complete`; clean authored text
does not trigger a false positive.

Table rendering groups only source-contiguous rows; blank lines separate tables.
Projection uses the actual header width and optional outer-pipe grammar: padded
empty cells add no source text and truncated cells grant no DOM mapping. Separator
rows remain explicit gaps; pipe prose without a header separator remains prose.
Within cells, including code spans, each escaped `\|` is a separate non-identity
segment mapping two canonical code points to one visible pipe. Adjacent identity
runs retain exact inner boundaries, including emoji and CRLF source offsets.
Clients must verify that transformation in table context, not globally strip
backslashes. Unsupported or unequal renderer/client mappings still use explained
page fallback; they never grant guessed exact anchors. These rendering repairs
do not change canonical block ids, spans, hashes or resolver semantics.

When restoring CRLF coordinates for rendered list items, a normalized identity
run must split at each expanded newline: `one\r\n` maps as `one` followed by a
separate two-source-code-point to one-DOM-newline segment, never as one mixed-width
token. DOM text content retains the newline even when CSS displays it as a space.
This preserves the existing narrow client newline decoder and canonical offsets.

In-bundle Markdown links retain fragments and use the same browsing context on
concept pages, including bare sibling `other.md#heading` and `#heading` links.
On the source concept's own live/static page, self-fragments retain native
`#heading` destinations rather than reloading the document. Graph and single-file
payloads retain explicit source-document destinations because their host page is
not the source document.
External URLs keep their external-link classification and safe new-tab attributes.

Annotations v2 introduces no server resolution cache. A browser may memoize
only by the full projection key above and commits a result only if every key
component still matches. Canonical comment/doc refresh first removes obsolete
marks and then recomputes. Create, re-anchor, lifecycle/body mutation, concept
mutation, external edit, undo, SSE, polling, reconnect, navigation, resolver
version change, and any late result all trigger invalidation or an exact-key
check. Cache absence or failure falls back to canonical read; it never
fabricates an empty/resolved result. TTL, negative caching, stampede, and
stale-set concerns are intentionally absent in v2's initial implementation.

SSE frames use their globally unique numeric event sequence as `id`/cursor.
Create→claim→resolve yields three distinct cursors even for one comment. Replay
after overlap/rotation is oldest-first and deduplicated by cursor. A cursor older
than retained history, a gap, `resync`, or polling takeover requires canonical
`/__comments` plus document refetch; frames never reconstruct comment state.
The server subscribes before taking the replay snapshot, then deduplicates at a
high-water cursor so an event racing the replay/live handoff is delivered exactly
once rather than lost or duplicated.
The initial `ready` frame reconciles an uninitialized local revision through a
canonical fetch and never blesses stale server-rendered bytes. Its `doc_revs`
inventory comes from the current contained canonical concept inventory,
including concepts created after server start; values are canonical content
hashes, not bundle revisions or event identities. SSE validates query/cursor
inputs and completes fallible inventory/event preflight before committing HTTP
200. Polling applies the same strict query and revision-qualified resync
contract as SSE. Document
patches refetch canonical comments; pending v2 draft marks retain exact
occurrence identity only while their content revision remains current.
`agent_conflict` and `suggestions` have production EventSource consumers.

#### Fail-closed, keyboard, and security matrices

| Public input class | Required result |
|---|---|
| Empty/whitespace exact, id, row token, revision, or idempotency key | Reject `invalid_request`/`invalid_anchor`; never normalize into a value. |
| Duplicate JSON key, malformed object/type, boolean integer, negative/reversed/out-of-range offset | Reject before resolution or mutation. |
| Unsupported request/anchor key or schema | Reject; stored unknown fields alone are preserved. |
| Comment create/update unknown or duplicate key, wrong type, unsupported state, or combined update operations | Reject `invalid_request`; both routes are closed public schemas, and update permits exactly one of state/archive/body per row-token CAS. |
| Special characters | Accept only where the declared string grammar permits; render as escaped plain text. |
| Missing/stale/cross-concept comment or canonical concept | `comment_not_found` or conflict; request cannot redirect ownership. |
| Concurrent or duplicate request | Serialize under comment lock and apply row-token/idempotency rules. |

| Command/state | Route surface | Dialog/menu or editable owns focus | Expected focus/default behavior |
|---|---|---|---|
| Enter/Space on Re-anchor, Save, Cancel, Retry, Jump | Invoke focused control | Higher layer wins | Prevent default only when activating app control. |
| Escape while selecting or with valid draft | Cancel editor, discard draft | Higher layer wins; native editable Escape preserved unless editor owns it | Restore focus to originating Re-anchor control. |
| Escape during save | Do not pretend to cancel dispatched mutation | Higher layer wins | Stop waiting only; refetch canonical result. |
| Save success | Close editor | n/a | Focus Jump if resolved, otherwise comment card heading. |
| Save failure/conflict | Keep/open editor with persistent error; conflict requires refetch and reselection | n/a | Focus error summary, then return to selection control. |
| Jump | Only for resolved anchor | Higher layer wins | Move focus/scroll to mark and announce destination. |
| Copy, Delete, Backspace, Ctrl/Cmd+D, Alt/Option+number | No annotation command | Dialog/widget/editable/browser keeps ownership | Do not suppress native behavior. |
| SSE/doc update, card reorder/removal | Preserve focus by comment id when it remains | Higher layer wins | If removed, focus nearest card or panel heading; announce health change. |
| Conflict dialog over comments panel | Topmost conflict dialog exclusively owns Tab/Escape | Panel yields while dialog is open | One Escape closes only the dialog; native editable shortcuts remain unprevented. |

| Security boundary | Normative rule |
|---|---|
| CSRF / Origin / Host | Every POST requires `X-OKF-Token` and a non-empty allow-listed `Host`; when present, `Origin` must also be allow-listed. |
| Read-only mode | `--no-edit` returns typed `403 read_only`; no row/idempotency write. |
| Resource caps | Enforce 65,536-byte body cap before parse and string/context caps before block extraction or hashing. |
| Ownership/path containment | Derive concept from the latest row under the comment lock and read the contained current canonical resource under the defined lock order. The retained bundle is a cache, not membership authority; no caller path is opened. |
| Injection/secrets | Anchor text is escaped plain text. URL safety decodes character references before scheme classification, so named/numeric entities cannot hide a denied scheme while safe entity-bearing queries remain valid. Never log token, full request body, quote/context payload, or idempotency key. |
| Concurrency/atomicity | Use the shared per-comment cross-process lock and atomic append/rotation path. |

No-JavaScript pages render authored concept content and exactly one honest
fallback notice. They do not fetch or render comments, stored quote text, or
anchor health, and must not emit highlights, claim resolution, Jump, or expose
a Re-anchor control. Comments and every annotation interaction require the
JavaScript Studio client.

Release capture seeds unique stored comment-body and quote sentinels and
requires both absent from the complete no-JavaScript response HTML and DOM text;
selector absence alone is not proof.

The ten quality classifications for this implementation are: `state-ownership` (high),
`async-lifecycle` (high), `keyboard-focus` (high), `api-protocol` (high),
`contract-runtime-parity` (high), `fail-closed-validation` (high),
`security-boundary` (medium-high), `idempotency-retry` (high), and
`cache-invalidation` (high), plus `rendering-capture` (high). Direct proof is maintained in
`tests/test_annotations.py`, `tests/test_annotations_v2_backend.py`,
`tests/test_annotations_v2_http.py`, `tests/test_annotation_projection.py`, and
`tests/test_annotations_v2_browser.py`. The release-specific visual-evidence gate
is the git-ignored `captures/annotations-v2/` directory, which is never committed
to the published tree; after capture against the selected final
serialized tree, its schema-1 `manifest.json` must record the HEAD-verified
source revision, fully serialized consumed-input inventory, worktree and capture-script
digests, browser identity, actual per-artifact media/viewport, per-image SHA-256, release-proof
classification, and asserted scenario. Existing screenshot or manifest bytes
must not be treated as current-tree evidence before that regeneration. The
capture helper serves a temporary bundle and uses public HTTP APIs and browser
controls for posting, canonical reads, selection, and source-mutation recovery;
the conflict and save-failure frames deliberately inject `409` and `500`
envelopes at the browser HTTP boundary and are classified as injected-envelope
UI handling, not production-server failures; separate HTTP tests prove the real
server envelopes. Each inventory entry records exact path, regular-file or
symlink type, tracked boolean, Git mode, content hash, and symlink-target
semantics; unsupported special files and Git provenance failures fail closed.
Tracked modes come fail-closed from `git ls-files --stage` (`100644`,
`100755`, or `120000`); untracked regular files map executable bits to `100755`
or `100644`, and untracked symlinks use `120000`.
Capture recovery and publication hold one publication lock. Release publication
requires atomic directory exchange and fails closed when exchange is unavailable;
the two-rename recoverable fallback is not release authority. Rollback content
is retained through parent-directory fsync. Distinctness uses
staged bytes, and persisted body/text-quote sentinels are confirmed through
`/__comments` before no-JS checks. Capture and tests call one production
validator whose checks also run under `python -O`. These claims do not include Proposal/Diff
Review or performance work. Ordinary regression tests use temporary source and
evidence repositories, not previously captured screenshots. Passing those
tests proves validator/publication behavior, not visual release acceptance.
Recovery preflights every present sibling before mutation, quarantines the pinned
candidate under the trusted parent before mutation, immediately revalidates its
identity and complete content before publication, and never deletes the sole
prior proof. It accepts only validated complete real proof directories and
durably fsyncs cleanup. Inventory resolves path components
through trusted directory descriptors, opens leaves without following symlinks,
revalidates descriptor identity/type, and repeats membership before publication.
Published artifacts are hashed through the pinned output directory; exactly one
regular authoritative capture-script inventory row is required. Provenance snapshots are
deeply immutable; Git NUL framing and ignored-path membership are strict; and
transient publication lock files are excluded from the manifest inventory.

#### Post-r11 remediation quality proof matrix

This matrix is the durable proof index for the classified Annotations v2 risks.
        A reference names an exact pytest node. ReviewAll r11 reported FAIL; passing
tests do not establish current closure. A final ReviewAll should be rerun
against these bytes, and no result is claimed here before that review occurs.

| Classification | Final invariant | Exact proof nodes |
|---|---|---|
| State ownership | Every BFCache waiter remains pending until the latest queued resync reaches terminal state. Canonical tuples use discriminated anchor/event validation; re-anchor validates the complete sibling tuple before target adoption. | `tests/test_annotations_v2_browser.py::test_r11_resync_supersession_preserves_all_waiters_until_latest_terminal`; `tests/test_annotations_v2_browser.py::test_r11_bfcache_restore_with_superseding_resync_waits_for_latest_terminal`; `tests/test_annotations_v2_browser.py::test_r11_validate_staged_tuple_rejects_malformed_and_foreign_anchors`; `tests/test_annotations_v2_browser.py::test_r11_validate_staged_tuple_rejects_malformed_event_rows`; `tests/test_annotations_v2_browser.py::test_r11_loadcomments_rejects_concept_anchor_with_foreign_ref`; `tests/test_annotations_v2_browser.py::test_r11_loadcomments_rejects_unsupported_anchor_kind`; `tests/test_annotations_v2_browser.py::test_r11_loadcomments_rejects_malformed_event_row`; `tests/test_annotations_v2_browser.py::test_r11_reanchor_reconciliation_rejects_malformed_sibling_row`; `tests/test_annotations_v2_browser.py::test_r11_reanchor_reconciliation_accepts_valid_tuple` |
| Async lifecycle | Resync supersession preserves all waiters and resolves them only after the latest queued operation reaches a terminal state; BFCache reconnect waits for that shared completion. | `tests/test_annotations_v2_browser.py::test_r11_resync_supersession_preserves_all_waiters_until_latest_terminal`; `tests/test_annotations_v2_browser.py::test_r11_bfcache_restore_with_superseding_resync_waits_for_latest_terminal` |
| Keyboard focus | Narrow comments remain a modal overlay; wide comments are a non-modal complementary rail without a Tab trap. Conflict modal and native editable ownership remain explicit. | `tests/test_annotations_v2_browser.py::test_annotation_keyboard_preserves_native_editable_defaults`; `tests/test_annotations_v2_browser.py::test_conflict_dialog_exclusively_owns_keyboard_over_panel`; `tests/test_annotations_v2_browser.py::test_r6_overlapping_conflict_modals_settle_prior_owner`; `tests/test_annotations_v2_browser.py::test_r6_wide_rail_is_non_modal_complementary_and_no_tab_trap`; `tests/test_annotations_v2_browser.py::test_comments_drawer_stays_overlay_at_narrow_viewport` |
| API protocol | Public create is user-authored only; update permits exactly one typed operation and only `dismissed → open`; every lifecycle event carries its new row token; public legacy text is bounded like v2. | `tests/test_annotations_v2_http.py::test_public_comment_cannot_forge_agent_actor`; `tests/test_annotations_v2_http.py::test_comment_update_route_cas_success_and_conflicts`; `tests/test_annotations_v2_backend.py::test_external_lifecycle_allows_dismissed_reopen_but_not_claimed_or_resolved`; `tests/test_annotations_v2_backend.py::test_legacy_unique_resolution_returns_current_public_block_shape`; `tests/test_annotations_v2_http.py::test_public_comment_create_anchor_validation_matrix`; `tests/test_annotations_v2_http.py::test_public_comment_update_requires_and_enforces_row_token`; `tests/test_annotations_v2_http.py::test_polling_validation_and_resync_revision_parity` |
| Contract/runtime parity | Python capture/resolution, shared Markdown block IR, nested same-name HTML/multiline constructs, nested sibling offsets, HTTP projection, and browser canonical-visible occurrence checks agree. | `tests/test_annotations.py::test_capture_schema_blocks_and_original_resolution`; `tests/test_annotations.py::test_support_intersection_is_commutative`; `tests/test_annotations.py::test_extractor_v2_exact_fixture_matrix`; `tests/test_markdown_block_ir.py::test_projection_includes_ir_version`; `tests/test_markdown_block_ir.py::test_demo_bundle_projection_complete`; `tests/test_markdown_block_ir.py::test_r4_ir_list_continuation_line_visible`; `tests/test_markdown_block_ir.py::test_r4_ir_task_list_checkbox_removed`; `tests/test_markdown_block_ir.py::test_r4_ir_indented_code_no_trailing_newline`; `tests/test_markdown_block_ir.py::test_r4_ir_html_block_preserves_newlines`; `tests/test_markdown_block_ir.py::test_r4_ir_footnote_visible_matches_li_textcontent`; `tests/test_markdown_block_ir.py::test_r4_ir_escaped_table_pipe_correct_offsets`; `tests/test_markdown_block_ir.py::test_nested_same_name_html_capture_resolve_projection`; `tests/test_markdown_block_ir.py::test_multiline_comment_capture_resolve_projection`; `tests/test_annotation_projection.py::test_plugin_before_projection_initial_live_parity`; `tests/test_annotation_projection.py::test_static_build_plugin_before_projection_parity`; `tests/test_annotation_projection.py::test_r5_nested_sibling_segment_offsets_exact`; `tests/test_annotations_v2_browser.py::test_unicode_astral_combining_and_crlf_map_to_code_points`; `tests/test_annotations_v2_browser.py::test_projection_rejects_reordered_duplicate_occurrence` |
| Fail-closed validation | Malformed public schemas, pre-existing forged keys, HTML5 repair cases, wrong owner tags, ambiguous anchors, and corrupted storage never become a complete projection or mutation. | `tests/test_annotations_v2_browser.py::test_malformed_projection_rejects_whole_map`; `tests/test_annotations.py::test_overlapping_occurrences_and_conflicting_evidence`; `tests/test_annotation_projection.py::test_r7_table_foster_parenting_element_fails_closed`; `tests/test_annotation_projection.py::test_r7_implied_li_closure_fails_closed`; `tests/test_annotation_projection.py::test_r7_formatting_reconstruction_fails_closed`; `tests/test_annotation_projection.py::test_r7_foreign_content_svg_fails_closed`; `tests/test_annotation_projection.py::test_r7_no_preexisting_key_stays_complete`; `tests/test_annotation_projection.py::test_r7_browser_parity_html5_repair`; `tests/test_annotations_v2_backend.py::test_strict_directive_reader_rejects_complete_or_rotated_corruption`; `tests/test_annotations_v2_backend.py::test_pending_notification_payload_tamper_fails_closed_before_append` |
| Security boundary | Authentication masks lower-level details, no-edit/rate/body precedence is stable, post-load path swaps fail closed, and entity-encoded URL schemes are classified only after safe decoding. | `tests/test_annotations_v2_http.py::test_http_guard_precedence_body_cap_no_edit_and_rate_limit`; `tests/test_annotations_v2_http.py::test_raw_oversized_target_auth_mask_and_mode_precedence`; `tests/test_annotations_v2_http.py::test_post_load_symlink_swap_fails_closed_on_all_annotation_paths`; `tests/test_security.py::test_md_entity_encoded_link_scheme_is_blocked`; `tests/test_security.py::test_md_entity_encoded_image_scheme_is_blocked`; `tests/test_security.py::test_md_entity_query_amp_preserves_safe_link`; `tests/test_security.py::test_md_entity_decoded_link_preserves_safe_schemes_and_relative`; `tests/test_security.py::test_md_double_encoded_scheme_is_not_overdecoded` |
| Idempotency/retry | A single trusted receipt dirfd remains pinned across enumeration, consumption, and cleanup; directory substitution cannot redirect reads or unlink operations. | `tests/test_annotations_v2_backend.py::test_receipt_recovery_keeps_enumerated_directory_pinned_during_substitution`; `tests/test_annotations_v2_backend.py::test_receipt_cleanup_cannot_unlink_from_substituted_directory`; `tests/test_annotations_v2_backend.py::test_notification_receipt_directory_symlink_is_rejected_without_escape`; `tests/test_annotations_v2_backend.py::test_crash_receipt_temporary_is_removed_without_bricking_recovery` |
| Cache invalidation | Queued resync completion blocks BFCache reconnect; malformed drained work, partial tuples, foreign re-anchor/conflict documents, and malformed replies publish nothing. | `tests/test_annotations_v2_browser.py::test_r10_bfcache_restore_drains_queued_resync_before_connect`; `tests/test_annotations_v2_browser.py::test_r10_intersection_bfcache_resync_race_with_malformed_drain`; `tests/test_annotations_v2_browser.py::test_r10_loadcomments_rejects_non_array_comments`; `tests/test_annotations_v2_browser.py::test_r10_loadcomments_rejects_non_array_events`; `tests/test_annotations_v2_browser.py::test_r10_loadcomments_aborts_when_commitDocument_fails`; `tests/test_annotations_v2_browser.py::test_r10_reanchor_rejects_foreign_concept_doc`; `tests/test_annotations_v2_browser.py::test_r10_conflict_refresh_rejects_foreign_concept_doc`; `tests/test_annotations_v2_browser.py::test_r10_inline_reply_rejects_cross_concept_anchor` |
| Rendering/capture | Recovery quarantines a pinned candidate before mutation, immediately revalidates content, and never deletes the sole prior proof. Canonicalization reports convergence versus budget exhaustion; scrub uses the same bound and returned HTML is capability-free. Mandatory proofs cover exact deep CRLF offsets and an explicit two-level blockquote under LF and CRLF. | `tests/test_annotations_capture_manifest.py::test_r11_cleanup_quarantine_replacement_fails_closed`; `tests/test_annotations_capture_manifest.py::test_r11_restore_name_replacement_fails_closed`; `tests/test_annotations_capture_manifest.py::test_r11_restore_inplace_content_mutation_fails_closed`; `tests/test_annotations_capture_manifest.py::test_r11_unchanged_recovery_succeeds_and_leaves_no_quarantine`; `tests/test_annotations_capture_manifest.py::test_r11_recovery_never_deletes_sole_prior_proof`; `tests/test_annotation_projection.py::test_r11_canonicalize_returns_convergence_exhaustion_state`; `tests/test_annotation_projection.py::test_r11_overbudget_encoding_forces_incomplete_safe_fallback`; `tests/test_annotation_projection.py::test_r11_nested_encoded_forged_projection_key_scrubbed_capability_free`; `tests/test_annotation_projection.py::test_r11_clean_output_still_complete_no_exhaustion_false_positive`; `tests/test_annotation_projection.py::test_r10_multiple_nested_levels_crlf_source_offsets`; `tests/test_annotation_projection.py::test_r11_two_level_blockquote_exact_source_dom_proof` |
| End-to-end graph effect | The full agent comment→mutator→activity→graph flow proves the exact `tables/orders` source, requested target, and relation-label edge, then verifies it in the graph UI. | `tests/test_studio_iter2_e2e.py::test_full_agent_loop_e2e` |

#### Post-r11 async lifecycle proof matrix

| Async scenario | Owner and terminal rule | Exact proof nodes |
|---|---|---|
| Selection capture and UTF-16→code-point mapping | The active selection generation captures exactly one canonical span; entities, astral text, combining marks, CRLF, invalid boundaries, and over-cap selections fail closed. | `tests/test_annotations_v2_browser.py::test_unicode_astral_combining_and_crlf_map_to_code_points`; `tests/test_annotations_v2_browser.py::test_rendered_entity_captures_and_projects_full_canonical_token`; `tests/test_annotations_v2_browser.py::test_thematic_break_projection_validates_before_and_after`; `tests/test_annotations.py::test_capture_mapping_failure_and_revision_qualified_results` |
| Canonical fetch timeout, retry, and abort | The operation registry owns one absolute 10-second fetch deadline/AbortSignal across all attempts and delays; `500`/`429` retries are bounded and pagehide aborts even a pending retry delay. | `tests/test_annotations_v2_browser.py::test_canonical_get_retries_500_and_429_with_bounded_attempts`; `tests/test_annotations_v2_browser.py::test_absolute_deadline_spans_retries`; `tests/test_annotations_v2_browser.py::test_annotation_operation_registry_aborts_retry_delay_on_pagehide` |
| Supersession and route reuse | Superseded operations abort before retry/backoff, and a real A→B→A route sequence rejects delayed A work from the prior navigation epoch. | `tests/test_annotations_v2_browser.py::test_superseded_operation_aborts_before_backoff`; `tests/test_annotations_v2_browser.py::test_real_a_to_b_to_a_race_rejects_delayed_response` |
| Late canonical/comment completion | A late or reversed response cannot overwrite a newer document or comments generation. | `tests/test_annotations_v2_browser.py::test_post_success_stale_refresh_cannot_override_newer_document`; `tests/test_annotations_v2_browser.py::test_reversed_comments_response_cannot_overwrite_newer_generation` |
| Public create prepare/replay/restart | One idempotency key/body creates one physical comment and one notification across faults, concurrent processes, re-anchor, and restart; changed body conflicts. | `tests/test_annotations_v2_backend.py::test_create_append_before_event_replay_delivers_exactly_once`; `tests/test_annotations_v2_backend.py::test_create_reanchor_then_create_replay_and_unknown_prefix_extensions_survive`; `tests/test_annotations_v2_backend.py::test_spawn_public_create_same_key_restart_and_different_body_conflict`; `tests/test_annotations_v2_http.py::test_public_comment_create_anchor_validation_matrix` |
| Re-anchor prepare before append | A prepared WAL survives the fault/grace interval and resumes only the validated transaction. | `tests/test_annotations_v2_backend.py::test_legacy_token_and_wal_fault_expiry_cap_and_mismatch_matrix`; `tests/test_annotations_v2_backend.py::test_prepared_wal_retained_through_grace_then_tombstoned` |
| Re-anchor append before finalize | Replay discovers the transaction-bearing row, finalizes it, emits one notification, and does not append twice. | `tests/test_annotations_v2_backend.py::test_reanchor_wal_recovers_append_before_finalize_and_replays_once`; `tests/test_annotations_v2_backend.py::test_wal_reconciliation_rejects_transaction_row_metadata_mismatch` |
| Finalize before response / lost response | The same key/body replays success after finalize or a lost response; the browser refetches canonical state. | `tests/test_annotations_v2_backend.py::test_legacy_token_and_wal_fault_expiry_cap_and_mismatch_matrix`; `tests/test_annotations_v2_browser.py::test_lost_response_reconciles_and_retry_reuses_key` |
| Save retry | Retry preserves the identical key and body inside one absolute 15-second deadline; a changed body/key collision fails rather than mutating again. | `tests/test_annotations_v2_browser.py::test_failed_save_retry_reuses_identical_key_and_body`; `tests/test_annotations_v2_browser.py::test_post_absolute_deadline_spans_retries`; `tests/test_annotations_v2_backend.py::test_reanchor_idempotency_different_body_conflicts`; `tests/test_annotations_v2_http.py::test_http_idempotency_conflict_and_unavailable` |
| SSE resume, typed gaps, and polling backlog | Subscribe-first replay/live handoff delivers a racing event once; numeric cursors resume oldest-first; polling large-interval classification matches SSE. | `tests/test_annotations_v2_http.py::test_sse_subscribe_first_handoff_delivers_racing_event_once`; `tests/test_annotations_v2_http.py::test_last_event_id_replays_oldest_first_over_http`; `tests/test_annotations_v2_http.py::test_sse_more_than_10000_interval_resyncs_without_truncation`; `tests/test_annotations_v2_http.py::test_polling_more_than_10000_interval_has_sse_resync_parity`; `tests/test_annotations_v2_http.py::test_polling_cursor_pages_oldest_first_without_skipping_middle`; `tests/test_annotations_v2_backend.py::test_cross_process_event_cursor_race_restart_and_rotation` |
| Ready/poll/patch lifecycle | BFCache reconnect waits until the latest superseding resync is terminal; discriminated anchors, every event row, and the complete re-anchor tuple validate before commit. | `tests/test_annotations_v2_http.py::test_polling_validation_and_resync_revision_parity`; `tests/test_annotations_v2_browser.py::test_r11_resync_supersession_preserves_all_waiters_until_latest_terminal`; `tests/test_annotations_v2_browser.py::test_r11_bfcache_restore_with_superseding_resync_waits_for_latest_terminal`; `tests/test_annotations_v2_browser.py::test_r11_validate_staged_tuple_rejects_malformed_and_foreign_anchors`; `tests/test_annotations_v2_browser.py::test_r11_validate_staged_tuple_rejects_malformed_event_rows`; `tests/test_annotations_v2_browser.py::test_r11_reanchor_reconciliation_rejects_malformed_sibling_row`; `tests/test_annotations_v2_browser.py::test_r11_reanchor_reconciliation_accepts_valid_tuple` |
| Torn-tail append recovery | Before the next append, an incomplete active JSONL suffix is truncated under lock while complete malformed rows remain fail-closed; directives and events retain separate valid rows. | `tests/test_annotations_v2_backend.py::test_torn_tail_entirely_partial_truncates_to_zero`; `tests/test_annotations_v2_backend.py::test_torn_tail_with_complete_corruption_remains_fail_closed`; `tests/test_annotations_v2_backend.py::test_torn_directives_tail_repaired_through_post_comment`; `tests/test_annotations_v2_backend.py::test_torn_events_tail_repaired_through_append_event` |
| Navigation/pagehide/BFCache | Departed pages cannot commit late work; persisted restoration explicitly clears stale selection presentation and resumes from canonical state. | `tests/test_annotations_v2_browser.py::test_pagehide_fences_late_save_and_escape_cancel_restores_focus`; `tests/test_annotations_v2_browser.py::test_real_a_to_b_to_a_race_rejects_delayed_response`; `tests/test_annotations_v2_browser.py::test_r6_bfcache_restore_clears_selection_banner_and_body_class` |
| Undo/content restoration | Undo-equivalent canonical restoration leaves anchor intent stored and recomputes health against the restored revision. | `tests/test_annotations_v2_http.py::test_projection_recomputes_content_changes_and_restore` |
| Lifecycle/re-anchor races | Shared locks and owned-field merges preserve anchor/body/lifecycle invariants; external CAS and archive/reply/reopen/claim races produce one guarded winner. | `tests/test_annotations_v2_backend.py::test_concurrent_lifecycle_writers_merge_owned_fields_under_one_lock`; `tests/test_annotations_v2_backend.py::test_external_comment_cas_concurrent_claim_edit_one_wins`; `tests/test_annotations_v2_backend.py::test_concurrent_archive_reply_reopen_and_claim_dismiss_preserve_guards`; `tests/test_annotations_v2_http.py::test_public_comment_update_requires_and_enforces_row_token` |
| Publication fence and post ownership | A read started before a newer publication cannot roll it back; every composer post and inline reply settles; a cancelled post's late success only updates the list and keeps the newer graph draft; a pending post ignores selections; inline reply and edit text is locked while sending; changed text after an unconfirmed reply is not promised to be duplicate-free; Retry refresh never posts again; an unconfirmed composer post keeps its key only while its draft is the one sent, refusing new selections meanwhile, and a changed draft is sent as new with a warning; a 2xx with an unreadable body is unconfirmed; Back within the page orphans neither a composer post nor a reply; a same-revision re-commit is not a publication; a capture after a live patch, or a boot capture after a newer revision, rewrote the selection anchors only shown text; a stalled edit save unlocks; a failed confirming re-read after Back within the page or on the graph offers Retry refresh, never "Comment posted."; a post whose answer body stalls settles at its deadline; Esc on an unconfirmed exact draft sends it as new; Cancel and Dismiss keep an unconfirmed post's key; a quick action never replaces a draft being posted. | `tests/test_studio_async_fences_browser.py::test_cold_boot_comments_reader_cannot_roll_back_first_publication`; `tests/test_studio_async_fences_browser.py::test_capture_canonical_read_cannot_roll_back_first_publication`; `tests/test_studio_async_fences_browser.py::test_held_live_patch_cannot_overwrite_newer_commit`; `tests/test_studio_async_fences_browser.py::test_inline_reply_text_typed_after_dispatch_survives_success`; `tests/test_studio_async_fences_browser.py::test_inline_reply_edited_after_uncertain_does_not_promise_no_duplicate`; `tests/test_studio_async_fences_browser.py::test_inline_reply_interrupted_by_a_page_change_settles_uncertain`; `tests/test_studio_async_fences_browser.py::test_inline_edit_text_is_locked_while_saving`; `tests/test_studio_async_fences_browser.py::test_selection_does_not_retarget_a_post_in_flight`; `tests/test_studio_async_fences_browser.py::test_composer_post_losing_ownership_settles_not_pending`; `tests/test_studio_async_fences_browser.py::test_composer_post_whose_refresh_loses_ownership_still_finishes`; `tests/test_studio_async_fences_browser.py::test_cancelled_post_late_success_keeps_the_newer_graph_draft`; `tests/test_studio_async_fences_browser.py::test_retry_refresh_after_revision_change_never_reposts`; `tests/test_studio_async_fences_browser.py::test_unconfirmed_post_keeps_its_key_while_the_draft_is_the_sent_one`; `tests/test_studio_async_fences_browser.py::test_unconfirmed_exact_comment_after_a_page_change_is_sent_as_new`; `tests/test_studio_async_fences_browser.py::test_accepted_post_with_an_unreadable_answer_is_unconfirmed`; `tests/test_studio_async_fences_browser.py::test_in_document_back_does_not_orphan_posts`; `tests/test_studio_async_fences_browser.py::test_same_revision_recommits_do_not_leave_the_page_behind`; `tests/test_studio_async_fences_browser.py::test_capture_after_a_live_patch_rewrote_the_selection_anchors_only_shown_text`; `tests/test_studio_async_fences_browser.py::test_inline_edit_save_that_never_answers_unlocks`; `tests/test_studio_async_fences_browser.py::test_in_document_back_during_failed_confirmation_keeps_retry_refresh`; `tests/test_studio_async_fences_browser.py::test_in_document_back_during_failed_retry_refresh_keeps_retry_refresh`; `tests/test_studio_async_fences_browser.py::test_graph_post_whose_confirmation_fails_keeps_retry_refresh`; `tests/test_studio_async_fences_browser.py::test_stalled_post_body_settles_at_deadline`; `tests/test_studio_async_fences_browser.py::test_boot_capture_after_a_revision_rewrote_the_selection_anchors_only_shown_text`; `tests/test_studio_async_fences_browser.py::test_escape_removing_an_unconfirmed_posts_selection_sends_it_as_new`; `tests/test_studio_async_fences_browser.py::test_cancel_and_dismiss_keep_an_unconfirmed_posts_key`; `tests/test_studio_async_fences_browser.py::test_quick_action_does_not_replace_a_draft_being_posted` |
| Explicit cancellation and native ownership | Escape cancels only the owned editor state, normal comment actions are excluded from recovery, and reduced-motion/navigation behavior remains deterministic. | `tests/test_annotations_v2_browser.py::test_keyboard_activation_save_cancel_and_jump_focus`; `tests/test_annotations_v2_browser.py::test_anchor_recovery_excludes_normal_comment_actions`; `tests/test_annotations_v2_browser.py::test_reduced_motion_disables_smooth_jump_and_animation_before_navigation` |

The required final release screenshot set is exactly `resolved.png`,
`stale-after-mutation.png`, `selecting.png`, `conflict.png`,
`save-failure.png`, `focus-visible.png`, `mobile-320.png`,
`forced-colors.png`, and `no-js.png`, indexed by
`captures/annotations-v2/manifest.json`. The capture workflow in
`scripts/capture_annotations_v2.py` uses explicit checked failures and semantic
mobile/forced-colors readiness. The manifest records exact scenario identity,
image hashes, and actual viewport/media per artifact; independently verifies
`source_revision` against `HEAD`; and serializes exact path/type/tracked/mode/
hash/symlink/stage-0-object inventory entries for consumed tracked and untracked inputs while
excluding protected/local paths. Release closure requires the shared production
validator, including its `python -O` gate, to
pass against the selected final serialized tree. Existing image/manifest bytes
may be verified as historical evidence for their recorded source, but must not
be cited as current-tree proof after source drift. No
  stale-image or partial-set exception is part of this proof contract.
  `mobile-320.png` depicts the real document-peek state: drawer and scrim hidden,
  authored document and Return to comment control visible and in bounds, with
  return preserving editor/selection ownership.

### 12.2 Portable page-level feedback import (local operator only)

A portable feedback note is one page-level JSON file written offline and later
imported by a local Studio operator. The server-side parser, import routes,
provenance marker, and opt-in static/single-file export composer (§9) are
implemented. The local operator page `GET /__feedback-import` previews and imports one note
(see [HTTP routes](http_routes.md) and
[Import a portable feedback note](/how-to/import_portable_feedback.md)). Origin is
**unauthenticated**: anyone can write or edit a note file, byte-identical copies
of a page are indistinguishable, and claimed name/time/quote are unverified.
An imported note is a page comment: the preview heads any quote "Quote in the
note (unverified)"; the import help and confirm notice say the quote is shown
with the comment but is not highlighted or pinned to exact text; and the
imported card labels it "Quote in the note (unverified) — not highlighted on
the page". The Imported portable note chip is unchanged.

Note format v1 is closed; `scripts/okf_loom/portable_feedback.py` owns it:

```json
{"format": "okf-loom.portable-feedback", "version": 1,
 "note_id": "<32 lowercase hex>",
 "target": {"document": "tables/orders.md", "source_sha256": "<64 lowercase hex>"},
 "body": "…", "quote": "…", "claimed_author": "…",
 "claimed_at": "2026-09-22T10:15:00.000Z"}
```

`format`, `version`, `note_id`, `target`, and `body` are required. `quote`,
`claimed_author`, and `claimed_at` are optional, and when present must be non-empty
strings. The parser rejects any other top-level or `target` key, duplicate keys,
NaN/Infinity, a UTF-8 BOM, invalid UTF-8, lone surrogates, inexact types
(`version:true`/`1.0`), bodies over 32 KiB, a `body` that is not 1–8000 code points
with non-whitespace, a `quote` over 1000 code points, C0 controls other than LF/TAB
in `body`/`quote`, and a `claimed_author` over 80 code points or containing
C0/DEL/C1/bidi controls. `claimed_at` must be
`YYYY-MM-DDTHH:MM:SS[.mmm]Z` and a real date. `target.document` must be a
canonical concept document; reserved `index.md`/`log.md` are rejected. Text is
stored exactly as received. `note_sha256` is the SHA-256 of the canonical JSON
serialization of the parsed note, with sorted keys, compact separators, and
non-ASCII kept, so reformatting the same note does not change it.

`source_sha256` is the SHA-256 of the target's **exact on-disk bytes**.
`canonical_source.read_canonical_document_bytes` reads them with stable no-follow
admission: visibility, trusted root identity, `O_NOFOLLOW` walk, 16 MiB cap, and
a before/after `fstat` check. It is not the hash of newline-normalized loaded
text, and it is not the normalized annotation/section revision. A CRLF file
therefore has a different hash than its LF-normalized text.

`POST /__feedback-import/preview` and `POST /__feedback-import/confirm` take the
raw note bytes (never the generic JSON body parser). These are local-operator
routes. They are in no public route allowlist, and the check order is:

1. Public ingress is refused before the body is read.
2. Token plus Origin/Host (`403 forbidden`).
3. A Studio must be attached (`503 unavailable`).
4. `--no-edit` gives `403 read_only`. This applies to preview as well, and
   nothing is written.
5. Local only (`403 local_only`): both socket ends must be loopback on the
   private listener, and there must be no attached tunnel process/URL, no public
   ingress listener, and no non-loopback bind. A quick-tunnel client connects from
   loopback, so a loopback peer alone does not prove the operator is local.
6. A declared body over 32 KiB gives `413 note_too_large` and closes the
   connection. Steps 1–6 run before any body byte is read; steps 2–5 are checked
   again at dispatch.
7. `Content-Type: application/json`, otherwise `415 unsupported_media_type`.
8. Strict parse, otherwise `400 invalid_note` with `fields.note` = a stable reason.
9. Idempotent replay keyed on `pfn.<note_id>`, with the fingerprint request
   `{kind:"portable_feedback_import", note_sha256, document, source_sha256}`.
   Confirm replays a finalized (or durably reconciled prepared) create as
   `200 {ok, comment, replayed:true}` before any preview-token or target lookup,
   so replay works after a restart and after the target is deleted. The same
   `note_id` with different content gives `409 idempotency_conflict`.
10. Confirm only: the `X-OKF-Preview-Token` HMAC must verify, otherwise
    `409 stale_preview`.
11. Under the canonical document lock, the exact bytes are re-read. Missing or
    invisible gives `404 target_missing`, a hash mismatch gives
    `409 revision_mismatch`, and an I/O failure gives `503 unavailable`.
12. An ordinary idempotent comment create returns `201 {ok, comment, replayed:false}`.

Preview has no side effects. It never sweeps, finalizes, or delivers
notifications; it writes no create WAL, transaction result, receipt, or event.
It returns
`200 {ok, status, note, document, concept, source_match?, comment?, preview_token?, expires_at?}`,
where `status` is `ready`, `already_imported`, `conflict`, `revision_mismatch`,
or `target_missing`. A token is issued only when `ready`. Existing rows are
scanned for `portable_import.note_id` as an advisory hint after WAL retention
ends; this scan never grants authority. A preview token is
`b64url(payload).b64url(HMAC-SHA256)` over
`{v:1, sid:sha256(session token)[:16], nsha, doc, ssha, exp:now+600}`. Its key is
32 random bytes held only in the application-server process, so every token dies
on restart. The byte re-check in step 11 remains authoritative.

Confirmation creates an ordinary **open** page-anchored comment
(`anchor:{kind:"page"}`, `detail:{}`). Its `actor` is `user`, the same value the
local `/__comment` path records. The only provenance discriminator is the
server-set top-level row field
`portable_import = {schema:1, note_id, note_sha256, document, source_sha256,
claimed_author?, claimed_at?, quote?}`. It has no import time; the row `ts`
records that. Nothing else can set it: `/__comment` rejects unknown top-level
keys, `/__comment-update` and re-anchor use closed schemas, and CLI and events
never pass it. `detail.portable_import` is ordinary untrusted detail. Lifecycle,
body-edit, re-anchor, and archive rows copy the marker unchanged, and it
describes creation provenance only. The marker is **not** added to the closed
comment event/receipt schema; clients read it from `/__comments` rows.

The `pfn.` Idempotency-Key namespace is reserved. An ordinary `/__comment`
using it returns `400 reserved_idempotency_key`, so nobody can squat a note id.
Replay is bounded by the shared comment-create WAL, not guaranteed forever:
exact replay lasts 24 hours (cap 10k records shared with all creates), then a
fail-closed tombstone lasts up to 7 days (cap 10k), then a compact filter gives
a permanent `409` and may rarely give a false-positive `409` for a new id. Bursts
of comments can shorten every window. A prepared create whose row is durable is
reconciled on replay under the key → WAL-maintenance → directives locks, with
no concept lock, which is a subsequence of the global lock order. A crash after
append followed by target deletion therefore replays and never reports
`target_missing`.

### Ordinary regression versus selected release evidence

Run current annotation provenance/publication and retrieval regression without
browser capture or a historical checkout:

```bash
python -m pytest tests/test_annotations_capture_manifest.py tests/test_benchmark_retrieval_relevance.py -m 'not browser'
```

These checks need pytest, Git and Node.js for actual Python/portable-JavaScript
parity; they do not need Playwright or ancient repository objects. Annotation
tests use synthetic temporary source/evidence commits and image bytes. Retrieval
tests execute the current small fixture for ranking, citations, budget and
ordered search/Context Pack parity, and exercise report reconstruction against a
temporary locally recorded baseline. Neither fixture is a new release artifact.
The proposal runtime-identity gate remains ordinary material regression; it is
not classified as screenshot evidence or bypassed by this separation.

Historical verification is explicit and selects the matching evidence/source
context rather than the invoking working HEAD:

```bash
python -O scripts/capture_annotations_v2.py --verify-checkout /path/to/selected-evidence-checkout
python scripts/benchmark_retrieval_relevance.py --verify-report /path/to/recorded-report.json --current-checkout /path/to/recorded-source-checkout
```

Supply an independently prepared context; these commands do not repair or
regenerate evidence. The annotation command retains strict final validation:
the selected checkout must be clean and attached at the evidence-only commit,
with exactly one parent equal to the recorded source revision. Because
`captures/` is git-ignored, that evidence-only commit is prepared in the
selected evidence checkout with an explicit `git add -f captures/annotations-v2`;
it is never part of the published tree. It verifies the
complete inventory, script, artifact bytes and classifications. Unrelated work
in another checkout does not invalidate that historical context and cannot be
promoted by verifying it. The relevance command retains measured-source and
harness binding, fixture/metric checks, physical citation reconstruction and
pinned-baseline replay; that explicit historical lane requires the recorded
baseline objects and matching measured source bytes. Missing context or digest
drift is a verification failure, not permission to stamp an old report current.

No screenshot/report refresh is required merely to run ordinary regression.
Actual release claims still require appropriate source-bound evidence. The UI
overhaul's manual/browser-owned zoom and human accessibility acceptance remain
separate pending work; this separation neither completes them nor changes
capture retention, publication recovery, or access authority.

## 13. Session storage, undo, and caps

Live studio session state is derived/ephemeral and lives under `<bundle>/.okf-loom/session/` by default.
Recommended project hygiene is to keep `.okf-loom/session/` out of git unless a harness intentionally snapshots it for debugging.

### Private-directory admission

`SessionAuthority` pins state with directory descriptors and no-follow component
opens. Strict private components, including an explicitly approved private
ancestor for external state, require ownership by the effective UID and mode
**exactly `0700` or `02700`**. Missing private directories are requested as `0700`;
inherited setgid is retained, not cleared. Bundle-local session ancestors retain
their existing workspace-permission policy; this does not make them private.
External state still requires the existing trusted temporary/private ancestor
and containment checks; arbitrary external roots are not admitted.

[Proposal Review](/reference/proposal_diff_review.md#9-storage-integrity-rotation-and-migration)
uses the same exact private-directory modes for its repository and descendants,
including transaction stage/backup directories. Activation and these directory
adapters never chmod newly created, pre-existing, or substituted directories.
Group/other access and unsupported sticky/setuid bits fail closed. Proposal
authority records and the HTTP mutation token still require caller-owned,
single-link regular files with exact mode `0600` and no-follow access.

On Linux, directory setgid inherits the parent's group and propagates to newly
created subdirectories; it grants no group access and changes neither effective
UID nor effective GID. A different inherited GID is therefore not a refusal
condition. Admission trusts reopenable caller-owned private state, not proof of
which process created it: a safe directory already present before the first
observation may be admitted, while observed identity changes across admission
checks fail. Pinned descriptors do not confer exclusive ownership of a shared
parent namespace. This policy does not defend against root, hostile same-UID
writers, previously leaked descriptors, or later owner permission changes;
existing containment, identity checks, and recovery rules remain necessary.

Current session shape:

| Path | Purpose | Cap / invariant |
|---|---|---|
| `.token` | Per-session CSRF token, mode `0600`. | Regenerated by `scripts/okf-loom serve`; treat as secret. |
| `server.json` | Running server address: `{host, port, pid, started, url, tunnel_url, tunnel_public_edit}`. | Written by `serve` at startup, refreshed on tunnel attach/detach, removed on clean shutdown. Lets `scripts/okf-loom tunnel` find the live port and records the public capability; stale after a crash (detected by the connect failing). |
| `events.jsonl` | Activity/change/comment/presence event feed. | Active file cap from `studio.events_max_bytes` default 8 MiB; `studio.events_keep` default 7 rotated files. |
| `directives.jsonl` | Append-only full comment rows; latest row wins and v2 writers issue a fresh `comment_row_token`. | Active file cap 2 MiB; 3 files kept. |
| `.comment-reanchor-wal/` | Prepared/finalized re-anchor transactions, tombstones, and bounded fail-closed replay filter. | Finalized records use a 24-hour TTL and bounded cap; uncertain evicted outcomes cannot authorize a new append. |
| `.comment-create-wal/` | Prepared/finalized comment-create transactions, notification state, tombstones, and bounded fail-closed replay metadata. | Recovery proves one directive and exactly one transaction notification before marking delivery; uncertain outcomes cannot authorize a duplicate append. |
| `presence.json` | Current presence snapshot. | Stale presence recovers to idle after configured/default TTL. |
| `history/<hex>/<rev>.md` | Per-concept undo snapshots. | Ring-capped; pruned snapshots make undo return not-found/pruned status. |
| `history/_groups/<group_id>/manifest.json` | Group undo manifest. | Safe tokenised group ids only. |
| `.idempotency/<key>.json` | Legacy bounded retry cache infrastructure; not the replay authority for comment create/reply, re-anchor, or undo. | 10-minute TTL; max 10,000 files where a legacy caller explicitly uses it. |
| `.last-logged.json` | Cross-process write/event dedup. | Internal session bookkeeping. |

`POST /__comment` accepts an optional `Idempotency-Key`; keyed create/reply
replay is owned by the crash-consistent `.comment-create-wal/` protocol, while
keyless creates still use durable notification receipts. `POST
/__comment-reanchor` requires an endpoint-scoped key and uses
`.comment-reanchor-wal/`. In both WAL protocols, same-key/same-fingerprint
replay returns the established outcome and changed-body reuse conflicts; their
retention, tombstone, and uncertain-outcome rules are specified in §12.1.

`POST /__undo` restores snapshots as described below and in the HTTP route
reference. The current handler does not consume `Idempotency-Key`, so clients
must not infer retry deduplication from the legacy `.idempotency/` directory.
An already-restored target is reported as `409 already_undone`; that conflict is
the implemented replay observation, not a cached original response.

Undo is based on snapshots written before concept mutation.
Mutators support `--group-id` so several writes can be undone as one pass.
Every group id is path-token validated before it reaches history paths.

## 14. Trust and security model

The default trust boundary is loopback local use.
`scripts/okf-loom serve` binds `127.0.0.1` by default.
`--public` or a non-loopback host requires `--public-ack` or an interactive acknowledgement.

Guards:

- Every GET is public to the local server, except sensitive diff reads require the session token.
- Local/admin POSTs require the per-session `X-OKF-Token` and Origin/Host allow-list acceptance.
- `studio.allowed_hosts` defaults to `127.0.0.1` and `localhost` and is never empty.
- `--no-edit` makes the studio read-only; live reads stay on and mutating POST routes return `403`. `POST /__tunnel` is the one local/admin exemption for attaching a read-only share (server admin, not a bundle write); it still requires the admin token and Origin/Host acceptance and rejects public-edit opt-in.
- Active public tunnels use a dedicated loopback ingress listener with a generation-bound, server-owned identity. Public-vs-local classification never trusts `Host`, `CF-*`, or `X-Forwarded-*`. Missing/stale ingress state fails closed.
- Public requests are read-only unless the operator explicitly supplied `--allow-tunnel-comment` or `--allow-tunnel-edit`; default public HTML omits all mutation tokens and reports `edit: false`, `comment: false`. Comment-only accepts only create/reply; public edit HTML receives a distinct generation-scoped token accepted only on the five documented comment/content mutation routes. Public ingress always rejects tunnel administration, proposal routes, preview/presence, sensitive diff reads, and all PUT/PATCH/DELETE requests, including requests carrying a copied local admin token.
- Mutating bodies are JSON, size-capped, and schema-checked.
- `/__apply` accepts whitelisted update operations, not shell or arbitrary argv.
- `/__apply` validates required arguments and supplied optional arguments against
  the per-kind schema before mutation. Wrong types return `400` with
  `{"ok": false, "error": "bad_type:..."}`. Optional booleans (`all`,
  `create_if_missing`, `allow_forward_reference`) require JSON `true`/`false`,
  not strings, numbers, or null. Omitted options keep handler defaults; null is
  accepted only where explicitly nullable (such as `add_link.section`).
- Path-bearing config values must remain within the bundle root.
- Active viewer override/plugin code requires both bundle declaration and operator consent.
- The server embeds no LLM and does not talk to remote model providers.

These gates protect against other browser origins, DNS-rebinding-style abuse, malicious bundle code, accidental exposure, and data loss.
They are not a per-action leash on the user's own agent.

## 15. Extension API

There are three extension mechanisms:

1. Viewer file/template/static overrides under the configured viewer override directory.
2. Server-side `ViewerPlugin` hooks for render/build integration.
3. Client-side studio extensions via `window.okfLoomStudio.register(kind, config)`.

The current shipped `okfLoomStudio.register()` kinds are exactly:

| Kind | Current status | Contract |
|---|---|---|
| `panel` | Ships, wired | Adds a slide-over panel and studio-bar toggle. |
| `viewMode` | Ships, wired | Adds a read-only mode button to the Rendered/Source/Split switch. |
| `toolbar` | Reserved forward-compat | Accepted and warned, not wired. |
| `graphDecorator` | Reserved forward-compat | Accepted and warned, not wired. |
| `suggestionRenderer` | Reserved forward-compat | Accepted and warned, not wired. |

Reserved kinds must not be documented as usable current extension points.
They are accepted so future-targeted extensions degrade gracefully today, but the studio logs a warning stating the kind is reserved and not wired.

Example shipped panel extension:

```js
window.okfLoomStudio.register("panel", {
  id: "coverage",
  label: "Coverage",
  render(container, ctx) {
    container.textContent = "Open comments: " + ctx.state.comments.filter(c => c.state === "open").length;
  },
});
```

See `scripts/okf_loom/viewer/OVERRIDES.md` for the shipped extension reference.

## 16. Embedding in another harness

The repo is designed to be embedded by agents and harnesses without making okf-loom own the whole process.
Preferred embedding pattern:

1. Clone or vendor the repository.
2. Load root `SKILL.md` and focused resources from `resources/`.
3. Run CLI commands via source-checkout invocation.
4. Start `serve` only when an interactive studio is useful.
5. Let the external agent consume `scripts/okf-loom wait`, `scripts/okf-loom watch`, `/__events`, and `/__comments`.
6. Write through mutators or whitelisted update/apply paths.
7. Keep `.okf-loom/session/` ephemeral and out of normal commits.

Minimal Python read-only server embedding is possible with `ThreadingHTTPServer` and `OKFWikiHandler`, but mutating studio behavior requires deliberately attaching the same studio/session/token wiring that `scripts/okf-loom serve` creates.
Embedders that bind outside loopback must make the exposure decision explicitly.

See [/reference/embedding_guide.md](/reference/embedding_guide.md).

## 17. Verification expectations

A build-from-docs implementation must satisfy these acceptance checks:

- `okf_loom.SPEC_VERSION == "0.2"`, and `okf_loom.LOOM_VERSION == "2.0.0"` matches `scripts/okf-loom --version`.
- `validate`, `info`, `graph`, `search`, `context`, `discover`, `plan`, `update`, `repair`, `index`, `log`, `init`, `bootstrap`, `import`, `serve`, `tunnel`, `wait`, `watch`, `token`, `comment-claim`, `comment-reply`, `comment-resolve`, `comment-list`, `presence`, `render`, `build`, `capabilities`, `upgrade`, `write-concept`, `set-frontmatter`, `link-add`, `entity-add`, `update-section`, and `replace-text` exist on the CLI surface.
- Validation profiles and `--fail-on-broken-links` match this spec.
- Search modes are dependency-free and restricted to the six current modes; CLI/HTTP limits are `1..100`, and invalid or mode-inapplicable input fails closed.
- Context schema v1 preserves exact excerpts, absolute physical lines and citations, deterministic revision/item ordering, budget/count/truncation semantics, and JSON/Markdown item parity.
- Search and context are read-only over one immutable snapshot; invalidation/reload clears all derived passage, ranking, graph, capability, and revision caches coherently.
- Portable corpora are schema/version/revision/hash validated and expose six-mode retrieval with JavaScript without claiming arbitrary static no-JavaScript search.
- `okf-loom.config.yaml` fails closed on unknown enum, invalid bool, and path escape while preserving unknown keys.
- Mutators preserve unknown frontmatter, write atomically, and avoid duplicate/idempotency failures.
- Live studio emits SSE/events, supports polling fallback, preserves no-refresh patching, and exposes the documented routes.
- Before Annotations v2 is described as implemented, shared Python/JavaScript fixtures prove the closed schema, Unicode code-point/UTF-16 conversions, candidate-set outcomes, legacy ambiguity, and revision-qualified projection contract in §12.1.
- Annotations v2 mutation proof covers per-comment cross-process locking, row-token conflicts, unknown stored-field preservation, lifecycle/body/re-anchor races, required recoverable idempotency, append-before-response replay, and all typed `/__comment-reanchor` errors.
- Annotations v2 browser/integration proof covers every state/async/cache/keyboard/security matrix row, distinct globally unique SSE cursors with oldest-first replay/resync, no stale mark after every invalidation path, and honest no-JavaScript output.
- `comment_link` events are emitted when a resolve links activity ids.
- `directives.jsonl`, `events.jsonl`, history, and idempotency caps prevent unbounded session growth.
- Comment archive is a separate boolean track, root-only, gated on whole-thread-resolved, and auto-unarchives on reply.
- `okfLoomStudio.register()` ships `panel` and `viewMode`; other documented kinds are reserved and warned.
- Public bind, active-code, no-edit, CSRF, Origin/Host, body-size, and path-containment gates are fail-closed.

Suggested repo-local verification commands:

```bash
scripts/okf-loom --version
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom info docs-bundle --format json
scripts/okf-loom search docs-bundle "current spec" --mode lexical --format json
scripts/okf-loom context docs-bundle "current spec" --format json
scripts/okf-loom build docs-bundle --target static --out /tmp/okf-docs-site
```

## 18. Current source-of-truth references

This spec is intentionally self-contained for rebuilding current behavior.
Concise detailed references remain separate and should be kept aligned with this file:

- [/reference/cli.md](/reference/cli.md) for command flags and JSON output details.
- [/reference/http_routes.md](/reference/http_routes.md) for route status codes and request/response shapes.
- [/reference/comment_lifecycle.md](/reference/comment_lifecycle.md) for comment state and archive edge cases.
- [/reference/search_modes.md](/reference/search_modes.md) for ranking algorithms and result shape.
- [/reference/context_packs.md](/reference/context_packs.md) for schema-v1 context, citation, budget, snapshot, and portable contracts.
- [/reference/config_yaml.md](/reference/config_yaml.md) for config defaults and loader behavior.
- [/reference/capabilities.md](/reference/capabilities.md) for the current capability catalogue.
- [/reference/architecture.md](/reference/architecture.md) for module-level architecture.
- [/reference/embedding_guide.md](/reference/embedding_guide.md) for harness integration.
The upstream OKF SPEC 0.2 is the base wire-format reference.
This file governs current okf-loom behavior on top of that base.
