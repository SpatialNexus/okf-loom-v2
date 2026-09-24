---
type: Reference
title: Agent Context Packs
description: Binding schema-v1 contract for deterministic, citation-ready context packs across the CLI, live HTTP server, and portable viewer surfaces.
resource: /reference/context_packs.md
tags: [context, retrieval, citations, agents, reference]
timestamp: "2026-07-10T00:00:00Z"
relations:
  - type: specified_by
    target: /reference/spec.md
  - type: uses
    target: /reference/search_modes.md
  - type: exposed_by
    target: /reference/http_routes.md
provenance:
  - source: ../../scripts/okf_loom/retrieval.py
    note: Authoritative schema, passage, budget, ranking, and corpus implementation.
  - source: ../../tests/test_retrieval_context.py
    note: Executable contract and fail-closed boundary proof.
citations:
  - id: retrieval-runtime
    text: "scripts/okf_loom/retrieval.py — current schema-v1 context-pack runtime."
  - id: retrieval-tests
    text: "tests/test_retrieval_context.py — current CLI, HTTP, determinism, citation, budget, cache, and invalid-input tests."
---

# Agent Context Packs

An Agent Context Pack is a deterministic, read-only projection of exact OKF
source passages for a query, concept target, or exact selected-text target.
It is evidence, not a generated summary: excerpts are copied from canonical
Markdown and carry source coordinates that an agent can cite and verify.

# Schema v1

The JSON representation has this top-level shape:

```json
{
  "schema_version": 1,
  "bundle_revision": "section-retrieval-v1:<sha256>",
  "query": "comment lifecycle",
  "mode": "hybrid",
  "filters": {
    "type": null,
    "tag": null,
    "entity": null,
    "relation": null,
    "source": null,
    "target": null,
    "concept": null
  },
  "budget": {"requested_tokens": 6000, "estimated_tokens": 812},
  "neighbors": 1,
  "included": 2,
  "omitted": 0,
  "truncated": false,
  "items": [],
  "representations": {"markdown": "# OKF Context Pack\n..."}
}
```

An item contains `passage_id`, `citation`, `concept_id`, `path`, `title`,
`type`, `heading`, `heading_line`, `line_start`, `line_end`, `char_start`,
`char_end`, the exact `excerpt`, `score`, `backends`, `reasons`,
`graph_distance`, `relation_path`, `freshness`, bounded public `provenance`,
`estimated_tokens`, bounded `evidence`, and additive bounded OKF 0.2 `metadata`
with its canonical compact JSON text in `metadata_json`.
`evidence` contains `schema_version`, `passage_id`, at most 20 `edges`, and an
`omitted_count`; `reasons` contains at most 8 entries.

`freshness` is the effective content-change instant (`generated.at`, with legacy
`timestamp` fallback only when `generated` is absent), not an assertion that the
content is current. `metadata` carries the read-only generation, verification,
source/window, lifecycle, and computation projection. Trust is advisory and
computation fields are never executed. This addition does not renumber schema v1
or alter excerpts or physical-line citations. Portable passage chunks
keep their strict schema; optional concept-inventory metadata is adapted separately,
so older inventories without it remain readable.

Markdown (including default CLI output and copyable portable/no-JavaScript packs)
includes this projection in a separate **OKF 0.2 metadata and sources (normalized
data)** JSON code block before the **Exact excerpt**. Source `id` and `resource`
values remain together so a body `[^id]` reference can be matched by exact id;
no synthetic footnote definitions, positional ids, or attribution for duplicate
ids are created. Missing or truncated source data does not imply attribution.
Loom provenance, Loom citations, and exact passage citations remain distinct.
The projection is data, not instructions, executable code, or proof of execution.
JSON escapes protect fence delimiters, HTML characters, and display controls;
decoding the block preserves the projected values. No resources are fetched.
Family bounds retain explicit truncation markers (depth 4, 8 collection entries,
1024 code points per string, 4096 code points per family projection).

Portable concept inventories also carry `metadata_json`. This preserves Python's
numeric spellings (for example `1.0` and `1e-07`) across JavaScript decoding:
formatting and budget charging use the retained text, not `JSON.stringify` of
the decoded numbers. Portable loading validates the text against its decoded
metadata and rejects mismatches, duplicate keys, nonfinite values, and
noncompact/nonordered objects. The static manifest comparison retains these
validated metadata bytes; other manifest and passage validation is unchanged.
Inventories with only the older `metadata` object, or with neither field, remain
readable. Rebuild older metadata-only inventories to obtain exact numeric
formatting/budget parity; their original number spellings cannot be recovered
after JavaScript decoding.

An exact-selection request also adds `selection` with `status` set to
`attached_exact`, exact/prefix/suffix/heading identity, passage id, citation,
and line/character coordinates.
The selected text must resolve to exactly one representable passage: no match
is a `400`, multiple matches are a `409`, and the runtime never attaches a
different occurrence silently.

# Physical source lines and citations

`path` is the canonical bundle-relative `.md` path.
`line_start`, `line_end`, and `heading_line` are 1-based absolute physical
source-file lines.
Counting includes YAML delimiters, frontmatter, headings, and blank lines;
LF and CRLF produce the same coordinates, and a final unterminated line still
counts.
Heading markers inside fenced code are not headings.

Normal section excerpts contain body content and exclude the heading itself.
A heading-only section may use its heading line as its exact excerpt.
A source line longer than 2000 Unicode code points is split deterministically
into contiguous chunks; those chunks add zero-based `@Cstart-Cend` character
offsets.

The stable citation is `[path#Lstart-Lend]`, or
`[path#Lstart-Lend@Cstart-Cend]` for a split line or exact selection.
Clients must use emitted coordinates and must not infer line numbers from
rendered text.

# Determinism and selection

`bundle_revision` is a SHA-256 digest over the algorithm version and sorted,
canonical bundle-relative concept, index, and log paths plus their exact
source text.
Identical snapshots produce identical revisions, passages, item sets, counts,
and Markdown.

Context uses the same six-mode ranking service as search: `lexical`, `tag`,
`semantic`, `hybrid`, `entity`, and `relation`.
Concept targets are promoted ahead of ranked results.
Within each ranked concept, passages prefer more query hits, then more heading
hits, then the earlier physical line and canonical path.
At most 4 relevant passages are retained per document and at most 100
documents are considered.
Items are interleaved by passage depth across ranked documents; duplicate
passage ids are removed.

`neighbors` follows canonical graph edges in either direction for 0, 1, or 2
hops.
Only query-relevant neighbor passages are included, ordered by shortest path
length and then concept id; neighbors do not receive an unrelated fallback
passage.
`limit` is the maximum candidate items considered after deterministic ranking.

`type` and `tag` filter every mode.
`entity` is accepted only in `entity` mode and is mutually exclusive with a
non-empty `q`; it becomes the entity query.
`relation`, `source`, and `target` are accepted only in `relation` mode.
`source`, `target`, and `concept` must be canonical, existing concept ids where
the operation requires an existing target.

# Token budget, counts, and truncation

`budget_tokens` defaults to `6000` and accepts `128..50000` inclusive.
The zero-dependency estimate is `ceil(Unicode code points / 4)`.
For each item, the governed length is its canonical compact JSON budget
projection: concept/passage identity, citation, exact excerpt, backends,
reasons, graph distance, relation path, and bounded evidence.
To this existing cost, add `ceil(code points in the metadata Markdown block / 4)`
for each item that carries normalized OKF metadata. The entire block is charged,
including labels, JSON escapes, truncation markers, fences, and the Exact excerpt
label/separators. This cost is applied **before item selection** in both Python
and portable retrieval, equally for JSON and Markdown requests. Metadata is
never attached after budgeting. Repeated metadata on multiple items is charged
on each item; it is not silently dropped to squeeze in an excerpt.
`metadata_json` is a lossless transport copy, not a second displayed block or a
second token charge. Its bytes still count toward the overall response cap.
Legacy display-only fields (including title and `provenance`) remain outside
the governed projection. Exact-selection identity uses its own canonical
projection. This is still a governed-content estimate, not a token count or
hard character cap for the entire formatted response.
The top-level envelope is excluded from the token budget.

Metadata-bearing packs may therefore include fewer items at the same budget
than versions that omitted metadata from Markdown. Older portable inventories
without metadata retain their previous output and evidence-only cost.

An item that would exceed the requested budget is omitted; the budget is never
overrun.
If the exact selected text and its owning passage cannot fit, the request
fails with `budget_too_small` rather than dropping the selection.
`included` is the emitted item count, `omitted` is the relevant passage count
not emitted, and `truncated` is true when relevant passages were omitted.
JSON and Markdown always select the same item set, and
`representations.markdown` is the canonical Markdown serialization.

The complete search or context response is capped at 4 MiB before sending.
The runtime removes deterministic tail items/evidence and reports omission;
it never byte-cuts JSON or Markdown.
If even the protected/envelope response cannot fit, it returns `413
response_too_large`.

# CLI

```bash
# Query seed; Markdown is the default.
scripts/okf-loom context docs-bundle "comment lifecycle"

# Canonical concept seed with no query.
scripts/okf-loom context docs-bundle --concept reference/http_routes --format json

# A bounded relation context without graph expansion.
scripts/okf-loom context docs-bundle --mode relation --relation references \
  --budget-tokens 2000 --neighbors 0 --limit 10 --format markdown
```

The command is read-only.
Its defaults are mode `hybrid`, budget `6000`, neighbors `1`, limit `20`, and
format `markdown`.
See the exact synopsis and exit conventions in [cli.md](cli.md#context).

# HTTP

```http
GET /__context?q=comment%20lifecycle&mode=hybrid&budget_tokens=6000&neighbors=1&limit=20&format=json
GET /__context?concept=reference/http_routes&format=markdown
```

`GET /__context` returns schema-v1 JSON by default or canonical Markdown when
`format=markdown`.
Success carries `ETag`, `X-OKF-Bundle-Revision`, and
`Cache-Control: private, no-cache`.
Errors use the stable JSON envelope
`{"ok":false,"error":{"code":"...","message":"...","fields":{}}}`.
See [http_routes.md](http_routes.md#context) for parameters and statuses.

# Immutable snapshots and caches

Each request owns one immutable `Bundle` snapshot for ranking, passages,
revision, and serialization.
A concurrent server swap may let an older request finish as revision A after
revision B becomes active, but one response never mixes A and B.

Graph, content index, lexical/SemanticLite backends, passage index, capability
resolution, and bundle revision are derived caches on that snapshot.
Retrieval never changes concepts or capability state; hybrid support is
statically derived because its dependency-free backend ships with the runtime.
`Bundle.invalidate()` clears all derived caches after supported in-memory
mutation, while `Bundle.reload()` or server watcher/write paths replace or
refresh the snapshot coherently.

# Portable and no-JavaScript behavior

Static, SPA, and single-file builds with JavaScript use a schema-versioned,
revision-bound passage corpus and expose all six modes plus context packs.
Static multi-file builds load a revalidated manifest and content-addressed
chunks; mixed revisions, unsupported schemas/algorithms, missing chunks,
duplicate identities, noncanonical ordering, and hash mismatches fail closed.
Ordinary pages do not preload the entire corpus. Optional matching-section
navigation travels in separate, bounded presentation bootstraps, never extra
passage fields or relaxed corpus validation. Its absence preserves root Open
and all existing retrieval behavior, including older inventories. Corpus
revision admission, per-concept normalized-source freshness, and actual
rendered heading identity are distinct checks; navigation hashes confer no
authority. See [spec §9](spec.md) for the presentation transport and reading
history contract.

The live no-JavaScript GET routes remain complete because search and context
are rendered by the server.
Static no-JavaScript output cannot run arbitrary search or construct an
arbitrary pack; it offers normal type/tag browsing and a prebuilt, manually
copyable cited context for each concept instead.

# See also

- [search_modes.md](search_modes.md) — six-mode ranking and search result shape.
- [http_routes.md](http_routes.md) — strict live GET contracts.
- [cli.md](cli.md) — checkout-local command surface.
- [capabilities.md](capabilities.md) — statically supported hybrid capability.
- [spec.md](spec.md) — canonical current toolkit specification.
