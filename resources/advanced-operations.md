# Advanced operations

## Search and bounded context

All six modes are dependency-free (`lexical`, `semantic`, `hybrid`,
`tag`, `entity`, `relation`; the search default mode comes from
`okf-loom.config.yaml search.default_mode`):

```bash
scripts/okf-loom search docs-bundle "customer order" --mode lexical
scripts/okf-loom search docs-bundle "customer order" --mode semantic
scripts/okf-loom search docs-bundle "customer order" --mode hybrid
scripts/okf-loom search docs-bundle "orders" --mode tag
scripts/okf-loom search docs-bundle "Order" --mode entity
scripts/okf-loom search docs-bundle "" --mode relation --relation depends_on
```

`semantic` is SemanticLite: token + character-trigram cosine over title,
description, type, headings, tags, and entity labels. It is not a dense local
embedding backend. Search results carry section-level source evidence, exact
line citations, backend/reason details, and bounded relation evidence. In the
studio workbench, open **Evidence and context** for complete citations and
ranking reasons, use **Copy passage** or **Copy concept context**, or choose
**Build context pack** for the current result set. Every copy surface states its
source, scope, and estimated size before clipboard activation.

Build the same deterministic, citation-ready evidence for an external agent:

```bash
# Stable Markdown with exact [path.md#Lx-Ly] citations (default output).
scripts/okf-loom context docs-bundle "customer order" --mode hybrid \
  --budget-tokens 6000 --neighbors 1 --limit 20 --format markdown

# Versioned JSON, seeded from a canonical concept instead of a query.
scripts/okf-loom context docs-bundle --concept tables/orders \
  --budget-tokens 3000 --neighbors 0 --format json
```

Context defaults are hybrid mode, 6000 estimated tokens, one graph-neighbor
hop, an item limit of 20, and Markdown output. Bounds are 128–50000 tokens, 0–2
hops, and 1–100 items. Packs are read-only and deterministic for one
bundle snapshot. They select exact excerpts and query-relevant graph neighbors;
they do not invent summaries or call an LLM. Prefer them over dumping arbitrary
files into a prompt. Mode-specific filters are strict: `--entity` belongs to
entity mode, while `--relation`, `--source`, and `--target` belong to relation
mode.

Live search/context is server-backed. Static and SPA builds with JavaScript
load a versioned, content-addressed passage corpus; the single-file viewer
embeds it and therefore grows with the bundle. Static no-JS can browse authored
pages and canonical concept-only Markdown context but cannot run arbitrary
queries. Live no-JS GET search and canonical Context Pack links remain
available; SPA/single-file no-JS has no interactive retrieval.

See the [Context Packs reference](../docs-bundle/reference/context_packs.md),
[`docs-bundle/reference/config_yaml.md`](../docs-bundle/reference/config_yaml.md)
for every config key, and [`studio-agent-loop.md`](studio-agent-loop.md)
for the serve-time collaboration loop.

## Discovery, plans, and repair

```bash
scripts/okf-loom discover docs-bundle --out /tmp/okf-suggestions.json
scripts/okf-loom graph-quality docs-bundle --format json
scripts/okf-loom plan docs-bundle --out /tmp/okf-plan.json
scripts/okf-loom update docs-bundle --plan /tmp/okf-plan.json --dry-run
scripts/okf-loom repair docs-bundle --indexes # dry-run by default; --apply writes
```

Use `--scope ID,ID --neighbors` for fast per-change enrichment.
Use `graph-quality` after imports or large curation passes to distinguish
valid-but-thin OKF from a bundle that will read well in graph view.

## Viewer/build

```bash
scripts/okf-loom serve docs-bundle --no-open
scripts/okf-loom build docs-bundle --target static --out /tmp/okf-docs-site
scripts/okf-loom render docs-bundle --out /tmp/viz.html
```

Viewer overrides are documented in
[`scripts/okf_loom/viewer/OVERRIDES.md`](../scripts/okf_loom/viewer/OVERRIDES.md).

For release proof, `scripts/capture_viewer_proof.py` waits on the production
capture-readiness API and publishes PNG/JPEG files atomically. The separate
`scripts/benchmark_gold_standard.py` harness defaults to 100/1,000 concepts;
pass `--large-scales` for larger inputs and `--static-build` to measure static
output. Large static work requires both opt-ins. Its compatibility
`browser_graph_mount` metric measures canvas attachment only and reports
`semantic_ready: false`; use production capture readiness for semantic graph
proof.

The relevance harness can independently verify a tracked report without
regenerating it:

```bash
python scripts/benchmark_retrieval_relevance.py \
  --current-checkout . \
  --verify-report docs/design/gold-standard-relevance-2026.json
```

Verification recomputes the complete selected-checkout measured-source
manifest/digest, harness and fixture identity, tree/patch identity, aggregate
metrics, and ordered search/Context parity totals; tampering fails closed.

## Commit-bound skill archives

Use the standalone archive helper with a trusted local Git repository and the
intended revision. It reads the selected commit's tracked blobs, not dirty or
untracked checkout contents:

```bash
# Build: strict reproducibility verification is mandatory before publication.
python scripts/build_skill_archive.py --source . --revision HEAD --out /tmp/okf-loom.zip

# Verify an existing ZIP without modifying it (strict reproducibility by default).
python scripts/build_skill_archive.py --source . --revision HEAD --verify /tmp/okf-loom.zip

# Explicitly permit different valid DEFLATE encodings of identical committed bytes.
python scripts/build_skill_archive.py --source . --revision HEAD \
  --verify /tmp/okf-loom.zip --verification integrity
```

For repeatable verification, replace `HEAD` with the captured commit ID. The
archive root prefix must still match the source repository directory name.
The Python API is `verify_zip(path, source=repo, revision=commit,
verification="reproducible")`; `verification="integrity"` is the sole opt-in.
Unknown modes fail closed. CLI `--verification` requires `--verify`, which
cannot be combined with `--out`; no build mode bypasses strict verification.

Both modes require exact Git-bound membership, paths, order, regular-file modes,
timestamps, metadata, CRCs and decompressed bytes. Integrity relaxes only the
DEFLATE representation and its resulting compressed sizes/offsets, **not** the
compression method or ZIP profile. Local and central headers must agree and all
physical bytes must belong to the admitted records: gaps, overlaps, hidden or
duplicate members, trailing compressed streams, leading/trailing bytes,
descriptors, comments, extras, encryption and ZIP64 remain rejected.
An integrity pass does not establish byte reproducibility.

Artifact exclusions and redacted private-key scanning remain in force; integrity
verification cannot disable content scanning. The scan is heuristic, not proof
that arbitrary committed content contains no credentials. Builds retain their
existing verify-before-atomic-publication and recovery behavior.

## Capabilities and config

```bash
scripts/okf-loom capabilities
scripts/okf-loom capabilities --bundle docs-bundle --format json
```

The capability registry lives in
[`scripts/okf_loom/extensions.py`](../scripts/okf_loom/extensions.py).
