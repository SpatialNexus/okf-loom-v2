---
type: Reference
title: Capability registry
description: The OKF capability registry — tier model (core / recommended / optional), the full catalogue, opt-in mechanisms, and the capabilities command.
resource: /reference/capabilities.md
tags: [capabilities, extensions, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# Capability registry

Capabilities are okf-loom's forward-compatibility mechanism.
Upstream OKF 0.2 §4.1 permits producers to add arbitrary frontmatter keys
and consumers MUST preserve them round-trip; the capability registry
is how okf-loom makes those arbitrary keys **meaningful** without
requiring a spec change.

A capability is a named, versioned declaration of an OPTIONAL
behaviour okf-loom recognises. Bundles opt in by declaring
capabilities under `okf_extensions:` or simply by using the keys
they govern.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

OKF 0.2 generation, sources, verification, lifecycle, and computation metadata
are upstream optional families consumed directly by the shared metadata reader,
not new capability requirements. The older Loom `provenance` and `citations`
extensions remain separate. Computation fields never activate execution.

# Tier model

| Tier | Meaning | Activation |
|---|---|---|
| `core` | Always active. Format primitives okf-loom could not function without. | Unconditional. |
| `recommended` | Ships with okf-loom; no plugin or extra required. | Activate on declaration or on observed use. |
| `optional` | Recognised but may need an external provider, plugin, or explicit opt-in. | Activate on declaration or on observed use of governed keys. |

A capability is "active" for a bundle when at least one of these holds:

1. It is `core` tier.
2. It is declared under `okf_extensions:` in the bundle-root
   `index.md` frontmatter.
3. Any of its `frontmatter_keys` appear in any concept's frontmatter
   (auto-activation).
4. Any of its `body_sections` appear as a heading in any concept
   (auto-activation).

Unknown declarations (an id not in the registry) are surfaced as
`undeclared` for diagnostics, not errors.

# Catalogue

## Core (always active)

| Id | Governs | Description |
|---|---|---|
| `okf.cap.frontmatter` | `type`, `title`, `description`, `resource`, `tags`, `timestamp` | Parse YAML frontmatter; alias-collapse known key variants. |
| `okf.cap.links` | — | Resolve absolute (§5.1) and relative (§5.2) cross-links. |
| `okf.cap.index_md` | — | Synthesize and respect upstream §8 `index.md` directory listings. |
| `okf.cap.log_md` | — | Append to upstream §9 `log.md` update history. |
| `okf.cap.listings` | — | Auto-generate folder/tag listing pages in the viewer. |
| `okf.cap.search_lexical` | — | Zero-dependency lexical search (BM25 / TF-IDF). |
| `okf.cap.graph` | — | Force-directed graph view of concept cross-links. |
| `okf.cap.backlinks` | — | Reverse-link (cited-by) panels in the viewer. |
| `okf.cap.discovery_mentions` | — | Detect unlinked mentions of concept titles as discovery suggestions. |

## Recommended (ship with okf-loom; opt-in by declaration or use)

| Id | Governs | Description |
|---|---|---|
| `okf.cap.search_hybrid` | — | Statically supported hybrid lexical + SemanticLite search with reciprocal rank fusion (RRF); both dependency-free backends ship in the runtime. |
| `okf.cap.popovers` | — | Hover-preview popovers for cross-concept links in the viewer. |
| `okf.cap.log_timeline` | — | Render upstream §9 log entries as a navigable timeline view. |
| `okf.cap.tag_pages` | — | Synthesize per-tag listing pages and tag cloud. |

## Optional (require an extra or plugin, or activate on use)

| Id | Governs | Description |
|---|---|---|
| `okf.cap.typed_relations` | `relations` | Interpret `relations:` as a list of typed relation objects (`{target, type, detail, …}`). |
| `okf.cap.entities` | `entities` | Interpret `entities:` as named-entity tags. Entries may be bare strings or `{id, label, kind, aliases}`. Labels + aliases feed entity search. |
| `okf.cap.aliases` | `aliases` | Interpret top-level `aliases:` as alternate names for the concept. Consumed by search and the viewer; object-form aliases may set `discoverable: false` to opt out of unlinked-mention discovery. |
| `okf.cap.provenance` | `provenance` | Interpret `provenance:` as source/origin metadata for the concept. |
| `okf.cap.citations` | `citations` | Interpret `citations:` as reference citations. |
| `okf.cap.embeddings` | — | Emit per-concept dense-vector embeddings for semantic search. |
| `okf.cap.discovery_ner` | — | Use offline GLiNER NER to suggest entities in concept bodies. |
| `okf.cap.discovery_llm` | — | Use an LLM to suggest missing links, relations, and summaries. |
| `okf.cap.diataxis_types` | — | Recognise the Diátaxis taxonomy (`Tutorial` / `How-to` / `Reference` / `Explanation`) for `type` values. |
| `okf.cap.wikilinks` | body syntax `[[…]]` | Recognise Obsidian-style wikilinks (Loom extension). Resolved to concept ids and added to the graph as `form="wikilink"` edges. No frontmatter key. |
| `okf.cap.jsonld_export` | — | Export the bundle graph as JSON-LD (schema.org/Dataset where applicable). |
| `okf.cap.multi_bundle` | — | Treat multiple bundles as one federated graph. |

# Opt-in mechanisms

## Frontmatter declaration

Declare under `okf_extensions:` in the bundle-root `index.md`
frontmatter (upstream §12 version declaration plus Loom capability extension):

```yaml
---
okf_version: "0.2"
okf_extensions:
  - okf.cap.typed_relations
  - okf.cap.entities
  - okf.cap.aliases
  - acme.custom_taxonomy   # user-defined, registered via plugin
generated: true
---
```

Only the bundle-root `index.md` may carry frontmatter (see
[frontmatter.md § Reserved filename frontmatter](frontmatter.md)).
Non-root `index.md` files are pure listings and cannot declare
extensions.

## Auto-activation

A capability auto-activates the moment any of its governed
`frontmatter_keys` appears in any concept, or any of its
`body_sections` appears as a heading. No declaration is required.

Example: adding `entities:` to a single concept activates
`okf.cap.entities` for the whole bundle; entity search and the
SemanticLite profile (which weights entity labels + aliases) begin
honouring it.

## Special-case activations

| Capability | Special activation |
|---|---|
| `okf.cap.search_hybrid` | Statically derived as supported for every loaded bundle because the dependency-free `HybridBackend` ships with okf-loom. |
| `okf.cap.wikilinks` | Body syntax only. Activated on the first `[[…]]` use anywhere in the bundle. |

Search and context requests are read-only.
Successful, empty, invalid, or failed retrieval does not alter resolved
capabilities or source state.

# Resolving capabilities

## CLI

```bash
# Every capability okf-loom knows:
scripts/okf-loom capabilities

# Which are active for this bundle (declared + auto-activated):
scripts/okf-loom capabilities --bundle path/to/bundle
```

Both accept `--format json`.

## Library

```python
from okf_loom.extensions import default_registry
from okf_loom import Bundle

bundle = Bundle.load("path/to/bundle")
resolved = bundle.capabilities()   # ResolvedCapabilities
print(resolved.active_ids)         # set of active capability ids
print(resolved.undeclared)         # list of unknown declared ids

if resolved.is_active("okf.cap.typed_relations"):
    ...  # feature is on for this bundle
```

`ResolvedCapabilities.active_ids` returns the active set;
`is_active(cap_id)` is the per-capability check.

## Validation

| Check | Severity | When it fires |
|---|---|---|
| `capability.undeclared` | WARNING | A declared id is not in the registry. |
| `capability.inactive_use` | INFO | A concept uses a governed key without the capability declared (auto-activated). |

```bash
scripts/okf-loom validate path/to/bundle --checks capability_declarations
```

# Defining a custom capability

```python
from okf_loom.extensions import Capability, default_registry

default_registry().register(Capability(
    id="acme.custom_taxonomy",
    tier="optional",
    description="Recognise Acme's internal concept taxonomy.",
    frontmatter_keys=("acme_kind", "acme_domain"),
    body_sections=("Acceptance Criteria",),
))
```

The id must be namespaced (e.g. `acme.x`); bare names without a dot
are rejected at registration time.

A plugin that wants to **act** on the capability should register a
transformer/emitter (for viewer behaviour) or check
`resolved.is_active("acme.custom_taxonomy")` in its own code.

Runtime third-party plugin loading is wired through the
`okf_loom.viewer_plugins` entry-point group; plugins run in
registration order behind the two-layer active-code gate (see
[config_yaml.md § viewer.allow_active_code](config_yaml.md) and
[`/reference/architecture.md`](/reference/architecture.md)).

# Capability id rules

| Rule | Detail |
|---|---|
| Namespace | Built-ins start with `okf.cap.`. User-defined must contain a `.` (e.g. `acme.x`). |
| Stability | Ids are stable across releases; removal is a breaking change. |
| Versioning | Each `Capability` carries `min_spec_version` (default `0.1`). |

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [frontmatter.md](frontmatter.md) — the keys capabilities govern.
- [links.md](links.md) — `okf.cap.links`, `okf.cap.wikilinks`, `okf.cap.backlinks`.
- [search_modes.md](search_modes.md) — `okf.cap.search_lexical`, `okf.cap.search_hybrid`, `okf.cap.entities`, `okf.cap.aliases`.
- [context_packs.md](context_packs.md) — read-only context and immutable-snapshot contract.
- [cli.md § capabilities](cli.md#capabilities) — the CLI command.
- [cli.md § validate](cli.md#validate) — `capability_declarations` check.
- [/explanation/architecture.md](/explanation/architecture.md) — why the registry exists.
- [/explanation/zero_dependencies.md](/explanation/zero_dependencies.md) — why `embeddings` is optional and SemanticLite is the default.
- `okf-loom/scripts/okf_loom/extensions.py` — authoritative source.
