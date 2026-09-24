---
type: Explanation
title: Dependency-light philosophy
description: Why okf-loom keeps retrieval, hybrid search, context packs, portable validation, and serving on a dependency-light checkout runtime.
resource: /explanation/zero_dependencies.md
tags: [dependencies, rationale, search, embedding]
timestamp: "2026-07-10T00:00:00Z"
---

# Dependency-light philosophy

This essay explains why okf-loom keeps the primary runtime path free of
install steps. PyYAML is preferred when present, and the checked-in runtime ships
a conservative built-in YAML fallback for normal docs validation when it is not.
It is not
the clone/setup guide (that is
[Use okf-loom from a clone](/tutorials/install.md)) nor the search reference
(that is [Search modes](/reference/search_modes.md)); it is the
reasoning that connects the two.

The runtime path is the checked-in `scripts/okf-loom` helper.
Everything else okf-loom does — validating, section retrieval, hybrid search,
context packing, discovering, rendering, and the live collaborative studio —
is built on the Python standard library plus PyYAML when it is available.
Without PyYAML, the bundled fallback accepts the YAML subset used by OKF docs/config and fails closed on unsupported advanced YAML such as anchors, aliases, tags, directives, merge keys, and multi-document streams.

# Why it matters

A dependency-light baseline earns three properties that a richer
    dependency set would erode.

## Embeddability

The same checkout runtime runs in a CI runner, a meta-agent's subprocess, an
air-gapped notebook, and a long-running viewer inside another agent
harness. None of those environments is guaranteed to have a C toolchain,
network access, or an environment that can resolve a deep dependency
tree. A toolkit whose normal source-checkout path does not require a primary install step
fits everywhere; a toolkit that pulls in a markdown library, a search engine,
a web framework, and an embedding backend fits only where those resolve cleanly.

This is also what makes okf-loom **harness-agnostic**. The three
integration shapes — shell out to the CLI, import the library, or run a
long-lived server in a daemon thread (see
[Embed the viewer in an agent harness](/how-to/embed_in_harness.md)) —
all work without the host having to accommodate okf-loom's
dependencies. The embedding contract is one checkout plus `scripts/okf-loom`.

## Source-checkout usability

A dependency-light checkout is fast, deterministic, and reproducible. There
is no dependency conflict to debug, no transitive package to pin, and no
surprise platform wheel that fails to build. For a tool whose primary
audience includes agents cloning it into a sandbox and loading the local
skill resources, that friction matters: every failed setup is a failed task.

## Audit surface

Every hard dependency is code the user is trusting. A smaller dependency
tree is a smaller attack surface and a smaller review surface. For a
tool whose job includes *consuming bundles produced by arbitrary third
parties* (see [What OKF is for](what_is_okf.md)), keeping the trust
boundary narrow is a deliberate stance: okf-loom reads untrusted
markdown and YAML, using PyYAML when present or the conservative fallback otherwise, plus
its own containment guards (`paths.path_within_bundle`) rather than a
stack of third-party libraries each of which would need its own audit.

> The constraint is stated as a hard rule in the agent brief
> (`AGENTS.md`): do not reintroduce package publishing or primary dependency installs as the normal runtime path.
> New features must keep `scripts/okf-loom` usable from a checkout.

# What the constraint costs

Dependency-light design is not free. Three costs are worth naming honestly.

## No rich markdown library

okf-loom renders markdown with its own small stdlib renderer
(`viewer/markdown.py`) rather than a battle-tested library like
`markdown`, `mistune`, or `marked`. That renderer handles the common
cases — headings, tables, fenced code, links in both forms, wikilinks —
but it is not a full CommonMark implementation, and edge cases that a
mature library would handle for free have to be handled by hand.

## No native search index

There is no SQLite FTS table, no LanceDB store, no Pagefind shard. The
search corpus and section passage index are derived in memory and scanned by
the built-in backends. Aggregate work caps, result limits, response limits,
request concurrency, and deadlines make this fail closed; they are not a
promise that an unmeasured bundle size will meet a latency target. See the
[measured performance reference](/reference/architecture.md#performance-characteristics).

## No local embedding backend

The most consequential cost: there is no `sentence-transformers`, no
local vector store, no on-device embedding model. A dense/vector search
backend would normally be the way to get true semantic retrieval, and
okf-loom ships without one.

# How okf-loom works around each cost

Each cost is met with a workaround that preserves the constraint.

## A stdlib markdown renderer, progressively enhanced

The stdlib renderer is the source of truth for the rendered view and
the `/__preview` endpoint, which keeps a single canonical renderer (spec
§5, decision D2) rather than a second client-side engine. Rich content
— mermaid diagrams, syntax highlighting (highlight.js), and KaTeX math
— is added by **progressive enhancement from a CDN**: the scripts load
on demand only when a page contains an element of that kind, degrade
gracefully to the raw source if the CDN is unreachable, and add zero
overhead on pages that do not use them. The same renderers also run in
the graph detail panel and the single-file export (pinned CDN versions;
highlight.js via jsDelivr's `+esm` bundle).

This is a deliberate trade: the canonical render path stays
dependency-free and predictable, and the *optional richness* is layered
on at the browser where a CDN miss is non-fatal. The page is always
readable; the enhancement is a bonus, never a requirement.

## Lexical search in pure Python

The `LexicalBackend` is a field-weighted BM25 over title (5x) >
headings (3x) > description (2x) > body (1x) > tags (1x), implemented
in pure Python with no dependencies. It satisfies the common case —
"search by term" — without an index file, a build step, or a running
process. See [search_modes.md](/reference/search_modes.md).

## SemanticLite: a zero-dependency fuzzy-semantic layer

The most interesting workaround is `SemanticLiteBackend`. True semantic
search normally means embeddings; okf-loom ships a
**fuzzy-semantic** layer that needs none. It builds a per-concept
profile (title ×3, description ×2, type, headings, tags, entity labels
and aliases) and scores similarity as:

```
score = α · cosine(tokens)  +  (1 − α) · cosine(char-trigrams),   α = 0.6
```

The trigram half is what makes it robust to typos and morphology —
`orders` scores close to `order`, `customer` close to `customers`. It
is not a dense embedding, and it does not pretend to be; it is a cheap
fuzzy-semantic layer that helps the agent find candidates, and it
costs zero dependencies. The `HybridBackend` fuses lexical and
SemanticLite by Reciprocal Rank Fusion (k=60), again dependency-free.

Hybrid support is therefore a static property of the checkout, not state
activated by a query. Lexical and SemanticLite indexes are lazy, locked,
rebuildable views on one immutable `Bundle`; search and context calls are
read-only.

## Exact context without embeddings or generated summaries

`RetrievalService` complements concept ranking with a deterministic section
passage index over canonical Markdown. Each passage keeps its bundle-relative
path, absolute physical line range, optional character range for a split long
line, and the exact excerpt. Context packs select from those authored excerpts,
add bounded ranking and graph evidence, and emit identical item sets in JSON
and Markdown.

This is intentionally not retrieval-augmented generation inside okf-loom.
There is no model call, embedding download, vector database, or invented
summary. The runtime supplies bounded, citation-ready evidence; the consuming
agent decides how to reason over it. Token accounting uses the conservative
zero-dependency estimate derived from emitted Unicode text and metadata rather
than a model-specific tokenizer.

## Portable search as validated derived output

Static and SPA builds cannot query a Python process, so the build writes a
schema-versioned manifest and content-addressed passage chunks. The browser
validates schema, algorithm, revision, sequence, counts, hashes, canonical
passage identities, and graph data before publishing a corpus. Partial or
mixed generations remain unavailable rather than silently falling back to a
different ranking mode. A single-file export embeds the same normalized
corpus.

These files are derived output, not canonical sidecar state. Rebuilding after
an edit changes the bundle revision and chunk identities. The fixed-name
manifest is revalidated; immutable hash-named chunks can be cached. Ordinary
pages do not preload the corpus, and static no-JavaScript pages honestly offer
browsing and canonical per-concept context rather than arbitrary search.

Portability has an intentional cost. In the audited static-build harness, the
completed retrieval output measured 133.541787 ms/2,149,088 bytes for 100
concepts and 2,945.275413 ms/15,019,571 bytes for 1,000 concepts. Comparable
pre-tranche baselines were 92.255596 ms/1,676,433 bytes and
2,038.156329 ms/11,296,537 bytes. The post-tranche measurements are one sample
at each size, so they describe observed time and size, not a general speed or
scale guarantee. No post-fix 10,000- or 50,000-concept measurement exists.

## The agent is the true semantic engine

The deepest workaround is structural rather than algorithmic.
okf-loom expects a consuming agent or human to perform open-ended semantic
reasoning. The runtime's job is narrower: retrieve exact, bounded evidence and
preserve citations. It does not need a local model merely to prepare that
evidence.

> This is the key insight from
> [`/explanation/research.md`](/explanation/research.md): a local dense-embedding backend was prototyped and
> then **removed**, because the agent is itself the semantic engine.
> SemanticLite stays as a cheap candidate-finder; the heavy semantic
> work happens in the agent, not in okf-loom.

This is also why the `SearchBackend` Protocol exists: a user who
genuinely needs a local embedding backend (an air-gapped deployment
with no agent, a very large corpus, a specific retrieval task) can
implement one behind the same `--mode` dispatch and register it. The
constraint is on the *default*, not on the *capability*. See
[capabilities.md](/reference/capabilities.md) for the extension model.

# Where the one optional extra fits

There is exactly one optional extra, and it is narrowly scoped:
explicit browser-proof dependencies, which install Playwright. They exist for a single
purpose — capturing browser-end-to-end proof that the live wiki
actually renders: the graph mounts, concept pages load, search returns
results, and backlinks show. Without it, those tests skip cleanly so
the default suite stays dependency-light.

```bash
python -m pip install playwright
playwright install chromium
pytest tests/test_viewer_browser.py        # e2e; skips cleanly without the extra
```

This is the model okf-loom uses for anything that genuinely cannot be
done with the standard library: it goes behind an *optional extra* that
is strictly additive, degrades cleanly when absent, and never enters
the default install or the default test run. Browser proof is the only
thing that earns that treatment today; if a future feature needed a
native search index or a rich markdown library, the same pattern
(optional extra, clean skip, no default impact) is how it would land.

# Why the constraint is a feature, not a limitation

It is tempting to read "dependency-light" as "missing features."
okf-loom's stance is the opposite: the constraint is what makes the
features it *does* have portable, trustworthy, and combinable. A richer
dependency set would buy a more polished markdown render and a native
search index at the cost of an install that fails in exactly the
environments — air-gapped, sandboxed, agent-spawned — where okf-loom
is most useful.

The workaround for each cost is also a feature in disguise. The
stdlib markdown renderer plus CDN progressive enhancement gives a
single canonical render path. SemanticLite plus the agent gives a
semantic search story that needs no model download. The
`SearchBackend` Protocol gives an escape hatch for users who need more,
without imposing it on users who do not. In every case the constraint
forced a design that is simpler to reason about than the dependency-rich
alternative would have been — and that is the real reason okf-loom
keeps it.

# See also

* [Use okf-loom from a clone](/tutorials/install.md) — the clone-first
  setup and your first `scripts/okf-loom validate`.
* [Search modes](/reference/search_modes.md) — the six modes, their
  backends, and the `SearchBackend` Protocol.
* [Context packs](/reference/context_packs.md) — schema-versioned,
  citation-ready evidence without embeddings or generated summaries.
* [Capability registry](/reference/capabilities.md) — how optional
  extensions land without a spec change or a new dependency.
* [Embed the viewer in an agent harness](/how-to/embed_in_harness.md) —
  the three integration shapes the constraint enables.
* [`/explanation/research.md`](/explanation/research.md) — the comparative
  survey, including the note on why the local embedding
  backend was prototyped and then removed.
* [`/reference/architecture.md`](/reference/architecture.md) — the
  module map, including the performance characteristics of the
  dependency-free search path.

Return to [Explanation](/explanation/index.md).
