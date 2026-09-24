---
type: Reference
title: Search modes
description: The six search modes behind one `--mode` flag, their backends, and the `SearchBackend` Protocol for plugging in custom backends.
resource: /reference/search_modes.md
tags: [search, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# Search modes

okf-loom's `search` command and the live studio's `/__search`
endpoint dispatch to one of six modes behind a single `--mode` flag.
**All six modes are dependency-free** out of the box: lexical (BM25),
semantic (fuzzy-semantic SemanticLite), and hybrid (RRF fusion of the
two) require no extra packages.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

# Mode summary

The viewer uses novice-facing labels while preserving these backend values in
URLs and CLI commands: **Best overall** (`hybrid`), **Exact wording**
(`lexical`), **Similar meaning** (`semantic`), **Tagged with** (`tag`),
**Mentions entity** (`entity`), and **Connected by relation** (`relation`).

| Mode | Backend | What it ranks by | Activate |
|---|---|---|---|
| `lexical` (default) | `LexicalBackend` | Field-weighted BM25 (title 5× > headings 3× > description 2× > body 1× > tags 1×). | Always. |
| `tag` | `LexicalBackend` | Tag membership (case-insensitive); ties broken by title. | Always. |
| `semantic` | `SemanticLiteBackend` | Fuzzy-semantic similarity over a per-concept profile. `α·cosine(tokens) + (1-α)·cosine(char-trigrams)`, `α=0.6`. | Always. |
| `hybrid` | `HybridBackend` | Reciprocal Rank Fusion (k=60) of lexical ∪ semantic-lite. | Statically supported; retrieval is read-only. |
| `entity` | (in-module) | Match against concept id, title, description, entity labels, and aliases. | Activates `okf.cap.entities` / `okf.cap.aliases` when those keys are present. |
| `relation` | (in-module) | Filter typed-relation + markdown graph edges by `--relation` / `--source` / `--target`. | Activates `okf.cap.typed_relations` when `relations:` is present. |

All modes accept the cross-cutting filters `--type TYPE` and `--tag TAG`
(applied as post-filters).

# `lexical` — BM25

Pure-Python BM25 with field weighting. Default mode. Zero deps.

```bash
scripts/okf-loom search path/to/bundle "customer order"
scripts/okf-loom search path/to/bundle "user" --type "API Endpoint"
```

## Scoring

For query terms `t` in field `f` of document `d`:

```
idf(t, f)     = ln( (N - df(t, f) + 0.5) / (df(t, f) + 0.5) + 1 )
bm25(t, d, f) = idf(t, f) * ( tf(t, d, f) * (k1 + 1) )
                                / ( tf(t, d, f) + k1 * (1 - b + b * dl(d, f) / avgdl(f)) )
bm25(q, d, f) = Σ_t bm25(t, d, f)
score(q, d)   = Σ_f weight(f) * bm25(q, d, f)
```

| Symbol | Default | Meaning |
|---|---|---|
| `k1` | `1.5` | Term-frequency saturation. |
| `b` | `0.75` | Length normalisation. |
| `N` | corpus size | Total number of concepts. |
| `weight(f)` | title 5.0, headings 3.0, description 2.0, body 1.0, tags 1.0 | Field weight. |

The `+1` inside the `idf` log keeps idf strictly positive (the
standard variant).

## Determinism

Documents are scored in sorted concept-id order and the final ranking
tie-breaks by concept id. The same `(bundle, query)` always yields
the same ordering.

## Tokenisation

Lowercase, split on Unicode word boundaries, drop a small English
stopword set, split CJK runs into single-character tokens
(so `比特币` indexes as `["比", "特", "币"]`).

# `tag` — tag membership

```bash
scripts/okf-loom search path/to/bundle "#sales" --mode tag
scripts/okf-loom search path/to/bundle "sales" --mode tag
```

Matches `Concept.tags` case-insensitively. Sorted by title. The
`#` prefix is optional. Every hit carries a constant `score=1.0`
plus `matched_tags` listing the subset of the concept's own tags
that matched.

# `semantic` — SemanticLite

```bash
scripts/okf-loom search path/to/bundle "customer order" --mode semantic
```

**Not embeddings.** Combines token-level TF cosine with
character-trigram TF cosine over a weighted per-concept profile.

| Component | Detail |
|---|---|
| Token cosine | Shares the lexical tokenizer; rewards shared domain terms. |
| Trigram cosine | Robust to typos and morphology (`orders` ≈ `order`). |
| Profile string | `title ×3 + description ×2 + type + headings + tags + entity labels + aliases` (the last two only if `okf.cap.entities` / `okf.cap.aliases` are active). |
| Score | `α * cosine(tokens) + (1-α) * cosine(trigrams)`, `α=0.6`. |

SemanticLite is the cheap fuzzy-semantic layer that helps an agent
find candidate concepts. The agent itself (an LLM in opencode /
Claude Code / Codex) is the best true-semantic engine, so a local
embedding backend would be redundant.

# `hybrid` — RRF fusion

```bash
scripts/okf-loom search path/to/bundle "customer order" --mode hybrid
```

Reciprocal Rank Fusion of `LexicalBackend` and `SemanticLiteBackend`,
both zero-dep.

```
rrf(d) = Σ_backend 1 / (k + rank_backend(d))    with k = 60
```

Concepts missing from a backend contribute 0 from that backend.
Sort by `(-rrf, concept_id)`.
`okf.cap.search_hybrid` is statically supported because both dependency-free
backends ship in the runtime; running a query does not mutate capability state.

# `entity` — entity/alias match

```bash
scripts/okf-loom search path/to/bundle "order" --mode entity
```

Match against concept id, title, description, entity labels, and
aliases. Honours `okf.cap.entities` (the `entities:` frontmatter
key) and `okf.cap.aliases` (the top-level `aliases:` key). Useful
for "what concept represents this business term?" lookups when
synonyms and aliases matter.

# `relation` — typed-relation / graph-edge filter

```bash
scripts/okf-loom search path/to/bundle "" --mode relation --relation depends_on
scripts/okf-loom search path/to/bundle "" --mode relation --source tables/orders
scripts/okf-loom search path/to/bundle "" --mode relation --relation writes_to --target tables/orders
```

Filter typed-relation declarations (`relations:` frontmatter key)
plus markdown graph edges. The query string is optional when at
least one of `--relation`, `--source`, or `--target` is supplied.
Honours `okf.cap.typed_relations`.

| Flag | Effect |
|---|---|
| `--relation TYPE` | Keep only edges of this relation type. |
| `--source ID` | Keep only edges whose source concept id matches. |
| `--target ID` | Keep only edges whose target concept id matches. |

# Cross-cutting filters

| Flag | Applies to | Behaviour |
|---|---|---|
| `--type TYPE` | Every mode | Keep only concepts whose `type` equals this string (exact match). |
| `--tag TAG` | Every mode | Keep only concepts carrying this tag (case-insensitive). |
| `--limit N` | Every mode | Maximum number of results (default `20`). |

# Result shape

```json
{
  "concept_id": "tables/orders",
  "title": "Orders",
  "score": 12.41,
  "snippets": ["…one row per completed customer order…"],
  "source_backend": "lexical",
  "matched_tags": [],
  "description": "One row per completed customer order.",
  "backends": ["lexical"],
  "reasons": ["ranked by lexical"],
  "evidence": {
    "schema_version": 1,
    "passage": {
      "passage_id": "tables/orders:L20-L24",
      "citation": "[tables/orders.md#L20-L24]",
      "path": "tables/orders.md",
      "line_start": 20,
      "line_end": 24,
      "excerpt": "Exact source passage..."
    },
    "edges": [],
    "omitted_count": 0
  }
}
```

| Field | Notes |
|---|---|
| `concept_id` | The dotted path with `.md` stripped (e.g. `tables/orders`). |
| `title` | Back-filled from the content index if the backend emitted a placeholder. |
| `score` | Backend-specific. Tag-mode results carry `1.0`. |
| `snippets` | 0..N ~120-char excerpts centred on first matches, ellipsized. |
| `source_backend` | `lexical` / `semantic-lite` / `hybrid` / `tag` / `entity` / `relation`. |
| `matched_tags` | Populated in tag mode; empty otherwise. |
| `description` | Back-filled from the content index for richer result context. |
| `detail` | Optional mode-specific matched fields or bounded edge detail. |
| `backends` | Additive backend identities; hybrid lists only the lexical and/or SemanticLite backends that actually contributed that result. |
| `reasons` | Additive bounded ranking explanations (at most 8). |
| `evidence` | Additive schema-v1 exact passage and bounded relation edges, with `omitted_count`. Passage coordinates are absolute physical source lines. |
| `metadata` | Additive bounded OKF 0.2 projection: generation, verification/trust, sources/windows, lifecycle, and computation fields. Advisory, not ranking authority or authorization. The search viewers show only its non-default trust, lifecycle, and freshness states on each result. |

The seven established fields remain present in every result.
Additive fields do not wrap or replace the compatibility result object.
Generation time falls back to legacy `timestamp` only when `generated` is absent.
Source signals are not converted into a credibility score. Metadata does not
change exact passage/line citations or execute a computation.

# `SearchBackend` Protocol

```python
@runtime_checkable
class SearchBackend(Protocol):
    name: str
    def index(self, content_index: ContentIndex) -> None: ...
    def search(self, query: str, *, limit: int) -> list[SearchResult]: ...
```

A backend indexes the canonical `ContentIndex` once and answers
ranked queries. `name` identifies the backend on `SearchResult`.

Backends consume the canonical
[`ContentIndex`](/reference/architecture.md) (the Quartz "one model"
pattern) so search, graph, and listings all read the same object.

# Caching

Graph, `ContentIndex`, lexical/SemanticLite backends, schema-versioned passage
index, capability resolution, and bundle revision are derived caches on one
`Bundle` snapshot.
Publication is lock-owned and coherent.
`Bundle.invalidate()` clears all of them after supported in-memory mutation;
`Bundle.reload()` refreshes canonical source and clears the same cache set.
Server writes/watchers swap or refresh the bundle snapshot rather than mixing
old and new derived state.
Search and context reads never activate a capability or mutate source state.

`okf_loom.search.clear_search_cache()` is retained as a
deprecated no-op for backward compatibility.

# HTTP entry point

The live studio exposes all six modes at `GET /__search?q=…` (HTML) and
`GET /__search?q=…&format=json` (bare compatibility JSON array).
HTTP defaults to lexical mode and limit `20`; the inclusive limit range is
`1..100`.
Unknown, duplicate, malformed, out-of-range, and mode-inapplicable inputs fail
closed.
See [http_routes.md](http_routes.md).

# Defaults

The bundle-root [`okf-loom.config.yaml`](config_yaml.md) `search.default_mode`
key selects the CLI `search` mode when no `--mode` is passed.
Its built-in default is `lexical`; allowed values are the six mode names
(fail-closed on unknown values).
HTTP search independently defaults to `lexical`, while CLI/HTTP context
defaults to `hybrid`.

# Portable surfaces

Static, SPA, and single-file builds with JavaScript expose all six
modes and context construction against a normalized, schema-versioned passage
corpus.
Unavailable capabilities remain explicit rather than silently falling back to
lexical ranking.
Static no-JavaScript output supports type/tag browsing and prebuilt cited
concept context, but arbitrary search requires JavaScript.
Live no-JavaScript search remains complete through the server-rendered GET.

# When to use which mode

| Goal | Use |
|---|---|
| Default keyword search | `lexical` |
| Tag browse | `tag` |
| Typo-tolerant / fuzzy match | `semantic` |
| Best of keyword + fuzzy | `hybrid` |
| Find the concept for a business term (with aliases) | `entity` |
| Find concepts connected by a typed relation | `relation` |

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [cli.md § search](cli.md#search) — the CLI command and its flags.
- [capabilities.md](capabilities.md) — `okf.cap.search_lexical`, `okf.cap.search_hybrid`, `okf.cap.entities`, `okf.cap.aliases`.
- [http_routes.md](http_routes.md) — `/__search`.
- [context_packs.md](context_packs.md) — schema-v1 context packs built from the same ranking and passage evidence.
- [config_yaml.md](config_yaml.md) — `search.default_mode`.
- [/explanation/zero_dependencies.md](/explanation/zero_dependencies.md) — why no embedding backend by default.
- [/how-to/discover_and_fix_gaps.md](/how-to/discover_and_fix_gaps.md) — `entity` / `relation` modes in the discovery workflow.
- `okf-loom/scripts/okf_loom/search.py` — authoritative source.
