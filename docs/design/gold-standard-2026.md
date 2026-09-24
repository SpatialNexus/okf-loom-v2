# Gold-standard retrieval and context release record (2026)

> **Historical scope:** this is the retrieval/context tranche record, not current
> maintenance authority. Its revisions, passes, counts, review requirements, and
> deferred roadmap describe that tranche only. Use the [maintenance plan](../../docs-bundle/explanation/maintenance_plan.md)
> for current priorities, the [spec](../../docs-bundle/reference/spec.md) for
> behavior, and the [proposal extension](../../docs-bundle/reference/proposal_diff_review.md)
> for today's experimental backend/UI boundary. Historical requirements do not
> gate ordinary approved maintenance; release claims still need their actual evidence.

## Purpose and release boundary

This document is the maintainer-facing design and release record for the Agent Context Packs and Full Search tranche.
It records what was present before the tranche, why this tranche was selected, what shipped in the working tree, how it was proved, and what remains.
The implementation keeps Markdown plus YAML frontmatter canonical and introduces no database, embedded model, required embeddings, framework, bundler, or package-install-first workflow.

The release includes:

- deterministic, citation-ready context packs;
- one section-aware retrieval service shared by search and context;
- all six search modes across CLI, live HTTP/SSR, static, SPA, and single-file JavaScript surfaces;
- exact physical source coordinates and evidence metadata;
- strict bounded read-only APIs;
- a schema-versioned, content-addressed portable passage corpus; and
- search, concept, exact-selection, and graph-neighbourhood context actions.

Historical boundary for this retrieval tranche: resilient text-quote annotation
recovery and proposal/diff review were deliberately deferred from it. Annotations
v2 was implemented later by the dedicated follow-on tranche recorded in the
current spec; it did not ship in this earlier tranche.

## Baseline revision, worktree, and command proof

The baseline revision was `c167909505da9abbac61e2d3889ebf51f4f1d947` on `develop`, equal to `origin/develop`.
The baseline worktree already contained unrelated local/runtime state: modified `.gitignore` and untracked `.aic/` evidence.
The tranche is uncommitted and is layered on that revision; the repository revision embedded in both baseline and post-fix benchmark reports is therefore the same commit hash.
No commit is represented by this record.

The baseline acceptance record was:

```text
1287 passed, 23 skipped
58 browser tests passed
```

The historical frozen acceptance record before the fresh ReviewAll remediation was:

```text
1511 passed, 23 optional skips in 350.34s
```

The immediately preceding historical full run had only the known pre-existing graph LOD
wall-clock flake (1510 passed, 23 optional skips). Its isolated rerun passed,
then that complete frozen-tree rerun passed. No threshold or product fix was
made.

After the first Groups A–E remediation, the then-frozen suite passed with
**1543 passed, 23 optional skips, 8 warnings in 369.26s**. The warnings are
Python 3.12 multiprocessing `fork` deprecations from the new eight-process log
append test, not product failures.

That count is historical after ReviewAll r2 remediation changed source and
tests. The then-frozen full suite passed with **1590 passed, 23 optional
upstream-fixture skips, 22 warnings in 427.65s (0:07:07)**. The warnings are Python 3.12 multiprocessing
`fork` deprecations from cross-process ownership tests, not product failures.

The preceding run had the known graph LOD external wall-clock flake; its
isolated rerun passed. Another prior run exposed a flawed synthetic pagehide
test; it was diagnosed and replaced with request-ID ownership proof that
passed 20/20. No production behavior was weakened and no timeout was relaxed.

That result is historical after ReviewAll r3 remediation changed source and
tests. The then-frozen post-r3 full suite passed with **1618 passed, 23
optional skips, 18 warnings in 365.53s**. The warnings are Python 3.12
multiprocessing `fork` deprecations from ownership tests, not product failures.

That result is historical after ReviewAll r4 remediation changed source and
tests. The then-frozen post-r4 full suite passed with **1639 passed, 23
optional skips, 20 warnings in 394.64s**. The warnings are Python 3.12
multiprocessing `fork` deprecations from ownership tests.

That result is historical after ReviewAll r5 remediation changed source and
tests. The then-frozen post-r5 full suite passed with **1650 passed, 23
optional skips, 18 warnings in 371.67s**. The warnings are Python 3.12
multiprocessing `fork` deprecations.

That result is historical after r6 imported-corpus contract remediation
changed source and tests. The then-frozen post-r6 full suite passed with
**1659 passed, 23 optional skips, 18 warnings in 371.59s**. The warnings are
Python 3.12 multiprocessing `fork` deprecations.

That result is historical after r7 Unicode/cancellation remediation changed
source and tests. The then-frozen post-r7 full suite passed with **1665
passed, 23 optional skips, 20 warnings in 373.28s**. The warnings are Python
3.12 multiprocessing `fork` deprecations.

That result is historical after r8 corpus/reload ownership remediation changed
source and tests. The then-frozen post-r8 full suite passed with **1671
passed, 23 optional skips, 18 warnings in 368.67s**. The warnings are Python
3.12 multiprocessing `fork` deprecations.

That result is historical after r9 failed-rollback/reload ownership remediation
changed source and tests. The final post-r9 frozen full suite passed with
**1672 passed, 23 optional skips, 18 warnings in 349.67s**. The warnings are
Python 3.12 multiprocessing `fork` deprecations.

Two test-harness corrections were included in that gate: a CSP-safe callable
wait predicate for presence and web-first `to_be_focused()` for asynchronous
focus transfer. They changed no runtime semantics.

The preceding historical run had 1542 passed and 23 optional skips plus one `file://`
`page.goto(..., wait_until="domcontentloaded")` host-contention timeout before
assertions. Its isolated rerun passed in 1.38s, then the complete frozen suite
passed. No timeout was relaxed and no product workaround was made.

The command surfaces and strict bundle validation were rechecked while preparing this record:

```bash
scripts/okf-loom --help
scripts/okf-loom search --help
scripts/okf-loom context --help
scripts/okf-loom validate docs-bundle --strict
```

The governing strict validation result was `PASS` with 0 errors, 0 warnings,
and 15 informational findings.
The post-remediation focused release gate recorded 447 passed and 11 optional skips.
Strict validation, JavaScript lint, and Python compilation passed.

An additional full-suite rerun during concurrent documentation work did **not** reproduce the final green count: it ended with 1406 passed, 23 skipped, and one static-link-integrity failure because concurrently added links in `docs-bundle/reference/context_packs.md` resolved as generated-site paths instead of repository paths.
That failure is outside this file and is not hidden by the earlier 1407/23 acceptance record.
This writer did not modify the concurrent file.

The full-suite command used was:

```bash
PYTHONDONTWRITEBYTECODE=1 .aic/tmp/sessions/aic-cx-okf-loom-v2-20260709235841/opencode/proof-venv/bin/python -m pytest -q
```

## Capability matrix before the tranche

The status below describes the verified pre-tranche state, not the desired end state.

| Capability | Before tranche | Evidence and consequence |
|---|---|---|
| Plain Markdown and YAML ownership | **Existing / strong** | The bundle model, parser, renderers, mutators, and OKF v0.1 specification already treated authored files as canonical and preserved dependency-light checkout execution. |
| Six dependency-free search modes | **Existing at CLI/core** | Lexical, semantic-lite, hybrid, tag, entity, and relation ranking existed in `scripts/okf_loom/search.py` and the CLI; see `docs-bundle/reference/spec.md` and `docs-bundle/reference/search_modes.md`. |
| Live HTTP and search page parity | **Partial** | `GET /__search` was lexical-only with a fixed result shape; richer CLI modes and filters were not available through the live human/agent surface. |
| Static search parity | **Partial** | Static search used an approximate browser implementation and a monolithic-style corpus rather than the authoritative section/evidence contract. |
| Section passages and exact source locations | **Missing** | Search snippets did not carry canonical Markdown paths, physical line ranges, matched headings, or stable citation labels. |
| Deterministic bounded agent context | **Missing** | There was no `context` CLI command, `GET /__context`, schema-versioned pack, hard token budget, or query-relevant graph-neighbour selection. |
| Graph exploration | **Existing / strong** | Graph data and the Map, Flow, Bridges, Recent, and Focus views already supported relationship exploration; the tranche reuses this graph rather than redesigning it. |
| Comments, activity, and undo | **Existing / strong** | Passage comments, the agent queue, attributed changes, and undo existed and remain compatible. |
| Annotation recovery after edits | **Partial** | Existing text replay could choose the first duplicate match; it lacked the proposed version-2 text-quote anchor and explicit ambiguous/stale states. |
| Broad proposal/diff review | **Missing as a first-class workflow** | Typed direct mutations and undo existed, but no versioned multi-operation proposal with per-operation accept/reject and grouped conflict-checked apply existed. |
| Database/editor source of truth | **Intentionally rejected** | A required DB or proprietary sidecar would weaken ordinary Markdown ownership and portability. |
| Embedded chatbot, LLM, or required vector service | **Intentionally rejected** | Retrieval must return authored evidence without invented summaries and must work without a provider or network service. |
| Framework rewrite, Node bundler, package-install-first runtime | **Intentionally rejected** | The checked-in, dependency-light Python and static-asset architecture is a product constraint, not a temporary omission. |

## Prioritization and selected tranche

Candidates were ranked using:

```text
(impact × differentiation × evidence × reach)
────────────────────────────────────────────
(cost × compatibility risk × maintenance)
```

### A. Agent Context Packs — selected

Context packs filled a complete product gap for external agents and humans copying bounded evidence.
They could reuse the existing ContentIndex and Graph while adding exact passages, deterministic selection, hard budgets, provenance signals, and stable citations without an LLM.

### B. Full Search — selected

Full Search closed an already visible parity gap: six modes existed in the core, but live HTTP and portable surfaces did not expose one authoritative result contract.
Implementing A and B together created one retrieval seam instead of separate search and context algorithms.
That shared seam reduced drift risk and gave every surface the same passages, coordinates, filters, and evidence vocabulary.

### C. Resilient annotations — deferred from this historical retrieval tranche

Annotation recovery is the next priority because first-match replay can silently attach to the wrong duplicate text.
It was not bundled into this retrieval release because safe recovery needed a separately versioned anchor schema, ambiguity handling, explicit re-anchoring, and a dedicated mutation/lifecycle proof matrix. The later Annotations v2 follow-on tranche supplied those pieces.

### D. Proposal/diff review — deferred

Proposal review has high value for broad changes, but it requires a durable transactional and idempotent proposal foundation, conflict semantics, partial acceptance, grouped apply, and a new review surface.
Adding it here would have expanded a read-only retrieval tranche into a high-risk mutation release.

## Implementation map, APIs, schemas, and surfaces

### Authoritative runtime seam

[`scripts/okf_loom/retrieval.py`](../../scripts/okf_loom/retrieval.py) is the authoritative dependency-free implementation.
`RetrievalService` operates over one `Bundle` snapshot, derives section passages, delegates ranking to the existing six-mode search implementation, enriches results with evidence, and builds deterministic context packs.
Search and context therefore do not maintain competing ranking algorithms.

[`scripts/okf_loom/model.py`](../../scripts/okf_loom/model.py) owns locked derived caches for graph, content, passages, revision, and search backends.
Invalidation and reload clear those caches coherently.
[`scripts/okf_loom/search.py`](../../scripts/okf_loom/search.py) preserves six-mode ranking and adds `backends`, `reasons`, and `evidence` without removing legacy result fields.

### CLI

[`scripts/okf_loom/cli.py`](../../scripts/okf_loom/cli.py) routes `search` through `RetrievalService` and adds:

```bash
scripts/okf-loom context BUNDLE QUERY \
  --mode hybrid \
  --budget-tokens 6000 \
  --neighbors 1 \
  --limit 20 \
  --type TYPE \
  --tag TAG \
  --format markdown
```

Both commands support `lexical`, `semantic`, `hybrid`, `tag`, `entity`, and `relation` modes.
Search defaults to 20 results with a 1..100 bound.
Context defaults to hybrid mode, a 6000 estimated-token budget, one graph hop, and 20 items; valid bounds are 128..50000 tokens, 0..2 neighbour hops, and 1..100 items.
Entity and relation filters are rejected outside their applicable modes.

### HTTP

[`scripts/okf_loom/server.py`](../../scripts/okf_loom/server.py) exposes two unauthenticated, read-only GET surfaces:

- `GET /__search` returns complete SSR HTML or a legacy-compatible bare JSON result array with additive evidence.
- `GET /__context` returns context schema v1 as JSON or deterministic Markdown.

Each handler captures one immutable bundle snapshot for retrieval, revision, and rendering.
Responses carry `ETag` and `X-OKF-Bundle-Revision` headers and use `private, no-cache`.
The server allows four concurrent retrieval operations by default and applies a 30-second deadline.

Strict parsing rejects malformed encoding, duplicate or unknown scalar parameters, unsupported modes/formats, mode-inapplicable filters, noncanonical concept targets, and out-of-range integers.
The request target cap is 64 KiB, query cap 4096 Unicode code points, filter cap 256 code points, per-excerpt cap 2000 code points, evidence-edge cap 20, and total search/context response cap 4 MiB.
Errors use stable `{ok:false,error:{code,message,fields}}` JSON where JSON is the negotiated response.

### Passage and context schema

A representable passage records a canonical bundle-relative `.md` path,
concept ID, title, type, heading and optional heading line, exact excerpt, tags,
entities, relations, freshness, provenance, citations, and 1-based physical
source lines. Passage-less graph/relation results remain valid concept-level
results but omit exact-passage evidence and controls.
Line coordinates include frontmatter and heading lines in the physical file; ordinary passage excerpts exclude the heading itself.
Stable labels use `[path#Lx-Ly]`, with optional character offsets for exact selections.

Context JSON uses `schema_version: 1` and includes:

- a deterministic content-derived `bundle_revision`;
- normalized query, mode, filters, neighbours, and budget accounting;
- included, omitted, and truncated counts;
- exact selected text metadata when a selection seed is used; and
- items containing exact excerpts, citations, scores, backends, reasons, graph distance/path, freshness, provenance, and evidence.

The zero-dependency item estimate is
`ceil(canonical budget-projection code points / 4)`. The projection includes
concept/passage identity, citation, excerpt, backend/reason metadata, graph
distance/relation path, and bounded evidence. Exact-selection identity has a
separate projection; display-only metadata and the envelope are excluded.
JSON and Markdown select the same item set, and the implementation does not invent summaries.

### Portable and human surfaces

[`scripts/okf_loom/render.py`](../../scripts/okf_loom/render.py) emits the evidence-rich search workbench, concept and selection context controls, default concept context, and portable corpus artifacts.
[`scripts/okf_loom/viewer/static/portable-retrieval.js`](../../scripts/okf_loom/viewer/static/portable-retrieval.js) implements the portable six-mode contract.
[`scripts/okf_loom/viewer/static/single-search.js`](../../scripts/okf_loom/viewer/static/single-search.js) supports embedded single-file retrieval.
The existing wiki, static-search, studio, and graph assets own suggestions, clipboard fallback, exact-selection controls, async cancellation/generation fencing, and graph-neighbourhood context actions.

Static and SPA output use a schema-v1 manifest plus content-addressed passage chunks keyed by bundle revision, chunk identity, and content hash.
Partial or mixed-revision corpora are never published as usable state.
Single-file output embeds the corpus.
Live no-JS search and context use complete native GET/SSR paths.
Static no-JS cannot run arbitrary retrieval; it retains browse and manual cited concept-context paths and hides inert JavaScript-only controls.

This reduced no-JavaScript behavior is the approved product contract: static
output provides honest browse and concept-only fallback, while SPA and
single-file output explicitly report interactive retrieval unavailable.
Arbitrary search and query-generated Context Packs require JavaScript or the
live server. No surface silently claims otherwise.

## Quality, security, compatibility, and rejected alternatives

### Decisions

- Retrieval is read-only; a hybrid query no longer mutates capability state.
- `okf.cap.search_hybrid` is statically supported/derived because the built-in hybrid backend is dependency-free.
- One immutable bundle snapshot prevents a response from mixing old content with a new revision during a watcher swap.
- Derived caches live on the snapshot and are cleared on invalidation/reload; no TTL is needed for immutable snapshots.
- Results are a bounded deterministic snapshot rather than a paginated mutable cursor.
- Context budgets and response limits fail closed rather than byte-cutting JSON or Markdown.
- Public projection bounds authored metadata and excludes session comments, tokens, private configuration, and filesystem roots.
- Static corpus publication validates schema, algorithm, revision, sequence, identity, hash, count, passage schema, and canonical order before exposing rows.
- Late suggestion, context, clipboard, corpus, and capture results are generation-owned and cannot update superseded UI.

### Rejected alternatives

- **Separate search and context engines:** rejected because ranking and evidence would drift.
- **Silent lexical fallback:** rejected because identical inputs could produce unexplained different rankings.
- **Required embeddings/vector database:** rejected because the dependency-free path and offline outputs are required.
- **LLM-generated summaries:** rejected because context packs must contain exact authored evidence only.
- **Database/editor canonical state:** rejected because Markdown ownership and ordinary Git review are non-negotiable.
- **Monolithic unversioned static index:** rejected because partial loading, stale mixing, and startup cost would be opaque.
- **A frontend framework or Node build:** rejected as disproportionate to the focused retrieval tranche.
- **Pagination without a deterministic cursor contract:** rejected; this release uses a hard result cap of 100.
- **Silent uncertain annotation reattachment:** rejected and deferred to the explicit text-quote design.

## Benchmark comparison

### Method and comparability

[`scripts/benchmark_gold_standard.py`](../../scripts/benchmark_gold_standard.py) generates deterministic ring-shaped bundles with fixed IDs, timestamps, text, tags, entities, links, and typed relations.
It measures full bundle load, six cold/warm search modes, one-file replacement plus full reload, graph model/payload construction, static build time/output size, and optional browser graph mount.
Static build time excludes `Bundle.load` and includes atomic output-tree replacement.

Baseline 100/1000 reports used three repeats.
The post-fix 100/1000 report used one repeat, so its median and p95 are a single observation and should not be treated as a stable distribution.
The generated bundle hashes match between baseline and post-fix runs, and the environment fields match (CPython 3.12.13 on the same reported Linux/x86_64 platform).

### Comparable static build time and size

| Concepts | Baseline time | Post-fix time | Time delta | Baseline bytes | Post-fix bytes | Size delta |
|---:|---:|---:|---:|---:|---:|---:|
| 100 | 92.256 ms | 133.542 ms | +41.286 ms (**+44.75%**) | 1,676,433 | 2,149,088 | +472,655 (**+28.19%**) |
| 1,000 | 2,038.156 ms | 2,945.275 ms | +907.119 ms (**+44.51%**) | 11,296,537 | 15,019,571 | +3,723,034 (**+32.96%**) |

These are regressions in static build cost and output size, not performance improvements.
They are the measured price of portable section passages, context artifacts, and retrieval assets in this implementation.
The cost is intentional for capability completeness but is a priority for follow-up optimization.

### Baseline-only scale evidence

The 10,000-concept baseline-only core run used one sample: bundle load 8,899.439 ms, cold/warm hybrid search 2,608.841/687.698 ms, and serialized graph payload 10,681,632 bytes.
Static build was skipped in that 10,000-concept report.

The 50,000-concept baseline-only core run used one sample: bundle load 53,370.043 ms, cold/warm hybrid search 14,569.499/3,441.486 ms, and serialized graph payload 53,401,632 bytes.
A separate baseline-only 50,000 static build recorded 9,093,103.660 ms, 50,016 files, and 535,057,542 bytes.

There is no before/after claim at those scales. A later current-only 10,000
run is recorded in the final Groups A–E closure below; no new 50,000 run was
started.

### Target-only batch observation

A test-run observation supplied during remediation measured common-term target-only default-context batches at approximately 0.0097, 0.0180, and 0.0384 seconds for 100, 200, and 400 concepts.
No durable JSON report containing those numbers was located.
They are therefore recorded as a **test-run observation**, not a release benchmark.
The durable source proves the structural direction instead: `default_concept_contexts()` scans the canonical passage index once and tests assert one index build without per-concept search.

## Browser, accessibility, no-JS, zoom, theme, and visual proof

Executable proof is concentrated in:

- [`tests/test_viewer_browser.py`](../../tests/test_viewer_browser.py): live/static/graph context ownership, superseded and pagehide responses, corpus retry, exact selection, no-JS states, suggestion timeout, clipboard failure/late settlement, capture readiness, 720/DPR2 and 320/DPR4 CSS viewport reflow, and editable combobox behavior;
- [`tests/test_render.py`](../../tests/test_render.py): static/SPA/single corpus generation, hash/revision validation, portable modes/context, exact evidence rendering, and live/static no-JS behavior;
- [`tests/test_retrieval_context.py`](../../tests/test_retrieval_context.py): canonical lines, budget parity, six modes, strict boundaries, snapshot swaps, invalidation, response caps, exact-selection ambiguity, and corpus validation; and
- [`tests/test_studio_iter1_browser.py`](../../tests/test_studio_iter1_browser.py): existing studio keyboard/focus and reduced-motion behavior, plus selection/comment non-regression.

The old 12-image set under
`.aic/tmp/frontend-remediation-proof/final-screenshots/` is explicitly
**historical and superseded**. It is local, non-release evidence and is not
used as current outcome proof. Current capture claims refer only to the
accepted six-image set under
`.aic/tmp/group-e-capture-proof/2026-07-11-viewer/`, supplemented by tracked
executable tests and the tracked capture command; those six ignored images are
also local non-release evidence rather than shipped artifacts.

Notable paths include:

```text
.aic/tmp/frontend-remediation-proof/final-screenshots/01-static-search-desktop-populated.png
.aic/tmp/frontend-remediation-proof/final-screenshots/02-static-search-320-populated-context.png
.aic/tmp/frontend-remediation-proof/final-screenshots/04-exact-selection-callout-clearance.png
.aic/tmp/frontend-remediation-proof/final-screenshots/05-clipboard-failure-anchored-result.png
.aic/tmp/frontend-remediation-proof/final-screenshots/06-live-nojs-default-context.png
.aic/tmp/frontend-remediation-proof/final-screenshots/07-static-search-dark-theme.png
.aic/tmp/frontend-remediation-proof/final-screenshots/08-static-nojs-search-honest-controls.png
.aic/tmp/frontend-remediation-proof/final-screenshots/10-live-nojs-graph-fallback.png
```

The captured and executable evidence supports keyboard combobox behavior, async/focus ownership, no-JS honesty, non-color invalid recovery, CSS viewport/DPR reflow, dark-theme rendering, clipboard fallback, and exact-selection clearance.
The final frontend proof records 39 browser tests and 69 render tests passing, with JavaScript lint and compile checks passing.

These are responsive-layout and accessibility-state claims only. Genuine
browser page zoom and text-only zoom automation is unavailable in the current
headless Chrome environment, so this release claims no automated zoom proof.
Manual Firefox zoom evidence is a disclosed deferred, non-blocking follow-up;
it is not a prerequisite for this release PASS.

The following was an explicitly **historical, pre-remediation** review state:

- quality-invariant review: **PASS**;
- Impeccable review: **PASS**;
- visual review trio: **no P0/P1 findings; P2 polish only**; and
- the last located code-review record: **NOT PASS**, with four degraded-state correctness findings.

Those historical gaps triggered remediation; they are not current principal
risks. Current tracked tests cover passage-less relation evidence honesty,
graph failure ownership, pagehide/BFCache lifecycle, hidden no-JS controls,
all five themes, forced colors, reduced motion, and CSS viewport/DPR reflow.
Ignored screenshots remain supplemental rather than executable proof.

## Migration and compatibility notes

- Existing OKF v0.1 Markdown and frontmatter remain readable and canonical.
- No source-data or bundle migration is required.
- Existing search result keys remain; `backends`, `reasons`, and `evidence` are additive.
- Context packs are a new `schema_version: 1` API and do not change the OKF document wire format.
- Existing comments, sessions, events, direct edits, and undo remain compatible.
- Hybrid capability semantics change: `okf.cap.search_hybrid` is now statically supported/derived and a read-only hybrid query no longer activates mutable capability state.
- CLI/HTTP bounds are stricter and fail closed; callers that relied on duplicate, unknown, out-of-range, or mode-inapplicable parameters must correct those requests.
- Static, SPA, and single-file builds gain generated passage manifests/chunks, context outputs, and retrieval JavaScript.
- Generated static assets are rebuildable projections, not authored source or required sidecar state.
- Static output is materially larger and slower to build at the measured 100/1000 scales.
- Static no-JS arbitrary search remains unavailable by design; browse and manual cited context remain available.
- No database, vector service, LLM, framework, bundler, or new required package is introduced.

## Risks and limitations

1. Genuine browser page zoom and text-only zoom automation remains unavailable
   in the current headless Chrome lane. CSS viewport/DPR reflow is not relabeled
   as zoom proof. Manual Firefox zoom is a disclosed deferred, non-blocking
   follow-up, not a release-PASS prerequisite.
2. No current 50,000-concept static run was started because the historical
   baseline exceeded 2.5 hours.
3. Graph LOD/CDN wall-clock, isolated `file://` navigation, and SSE timeout
   failures remain classified as environmental flakes after isolated/current
   reruns passed. No timeout threshold, retry count, or product behavior was
   relaxed.

## Prioritized roadmap

### 1. Resilient text-quote annotations — completed by the follow-on tranche

The follow-on tranche introduced version-2 anchors containing exact text, prefix, suffix, heading, block hash, and positions.
Recover only through deterministic contextual evidence; report zero/multiple plausible matches as stale or ambiguous and require explicit re-anchoring.
Keep old `{kind:text,ref}` records readable and prove insertions, moves, punctuation changes, duplicates, heading renames, and removals with zero silent wrong attachment.

### 2. Proposal/diff review

Add a versioned proposal object with expected revisions, typed operations, rationale, affected concepts, before/after diff, validation impact, and per-operation status.
Provide keyboard-accessible accept/reject review and conflict-checked atomic application of the accepted group, linked to activity and undo.
Retain direct mode for small, reversible edits.

### 3. Broader scale, performance, and quality polish

Repeat post-fix benchmarks at 100/1000 and add durable 10,000/50,000 measurements where operationally reasonable.
Profile and reduce portable corpus duplication, static file count, build time, and startup cost without weakening exact evidence or fail-closed corpus validation.
Add durable target-only batch reports, browser graph/startup metrics, forced-colors and five-theme matrices, cross-browser core flows, and a final independent code-review artifact.

At the close of the historical retrieval tranche, these were the exact next
three priorities, in order. The first is now implemented by the Annotations v2
follow-on tranche; the other two remain deferred:

1. resilient text-quote annotations;
2. proposal/diff review; and
3. broader scale/performance/quality polish.

## Final report

### What changed

The tranche adds deterministic section retrieval and context packs, full six-mode search across human and agent surfaces, exact source citations, additive evidence, strict read-only HTTP/CLI contracts, and versioned portable retrieval assets.

### Why

Humans and external agents needed one trustworthy way to find exact authored passages, understand why they matched, copy bounded context, and reproduce results without an embedded model or a second source of truth.

### Principal files

- `scripts/okf_loom/retrieval.py`
- `scripts/okf_loom/cli.py`
- `scripts/okf_loom/model.py`
- `scripts/okf_loom/search.py`
- `scripts/okf_loom/server.py`
- `scripts/okf_loom/render.py`
- `scripts/okf_loom/viewer/static/portable-retrieval.js`
- `scripts/okf_loom/viewer/static/single-search.js`
- existing viewer templates, CSS, and JavaScript integration files
- `tests/test_retrieval_context.py`
- `tests/test_render.py`
- `tests/test_viewer_browser.py`
- `scripts/benchmark_gold_standard.py`

### APIs

- `scripts/okf-loom search ...` with six modes, typed filters, strict limit, and additive evidence;
- `scripts/okf-loom context ... --format json|markdown`;
- `GET /__search` as SSR HTML or compatible JSON; and
- `GET /__context` as schema-v1 JSON or deterministic Markdown.

### Commands and results

- Baseline full suite: 1287 passed, 23 skipped.
- Historical pre-remediation frozen full suite: 1511 passed, 23 optional skips in 350.34s.
- Post-remediation focused release gate: 447 passed, 11 optional skips.
- Post-remediation frozen full suite: 1543 passed, 23 optional skips, 8 warnings in 369.26s.
- The 8 warnings are Python 3.12 multiprocessing `fork` deprecations from the eight-process log append test, not product failures.
- Frontend proof: 39 browser and 69 render tests passed; JavaScript lint, compileall, and strict docs validation passed.
- Historical concurrent-worktree run: 1406 passed, 23 skipped, one documentation static-link failure.
- `scripts/okf-loom validate docs-bundle --strict`: PASS, 0 errors, 0 warnings, 15 info.
- Historical pre-r3 frozen full suite: 1590 passed, 23 optional upstream-fixture skips, 22 warnings in 427.65s (0:07:07); 1543 is older history. Those historical 22 warnings were Python 3.12 multiprocessing `fork` deprecations from cross-process ownership tests, not product failures.
- Historical post-r3 frozen full suite: 1618 passed, 23 optional skips, 18 warnings in 365.53s.
- The 18 warnings are Python 3.12 multiprocessing `fork` deprecations from ownership tests, not product failures.
- Historical post-r4 frozen full suite: 1639 passed, 23 optional skips, 20 warnings in 394.64s.
- The 20 warnings are Python 3.12 multiprocessing `fork` deprecations from ownership tests.
- Historical post-r5 frozen full suite: 1650 passed, 23 optional skips, 18 warnings in 371.67s.
- The 18 warnings are Python 3.12 multiprocessing `fork` deprecations.
- Historical post-r6 frozen full suite: 1659 passed, 23 optional skips, 18 warnings in 371.59s.
- The 18 warnings are Python 3.12 multiprocessing `fork` deprecations.
- Historical post-r7 frozen full suite: 1665 passed, 23 optional skips, 20 warnings in 373.28s.
- The 20 warnings are Python 3.12 multiprocessing `fork` deprecations.
- Historical post-r8 frozen full suite: 1671 passed, 23 optional skips, 18 warnings in 368.67s.
- The 18 warnings are Python 3.12 multiprocessing `fork` deprecations.
- Final post-r9 frozen full suite: 1672 passed, 23 optional skips, 18 warnings in 349.67s.
- The 18 warnings are Python 3.12 multiprocessing `fork` deprecations.
- Test-harness-only corrections: CSP-safe callable presence wait predicate and web-first `to_be_focused()` async focus assertion; no runtime semantics changed.
- Current focused retrieval/render/browser/security/capture command: 480 passed, 11 optional skips, 8 warnings in 203.80s before the last external Group C additions; this is focused evidence, not the complete current gate.
- Combined current browser modules after the revisioned LOD fixture correction: `tests/test_studio_iter1_browser.py tests/test_viewer_browser.py` passed 161 in 335.89s.
- Final relevance source-manifest verification: `tests/test_benchmark_retrieval_relevance.py` passed 18 in 0.87s; search and Context Pack parity remain 7/7 and ranking metrics remain non-improvement/non-regression evidence.

### Metrics

- 100 concepts: static build +44.75% time and +28.19% bytes.
- 1,000 concepts: static build +44.51% time and +32.96% bytes.
- 10,000/50,000 post-fix comparisons: unavailable.
- Target-only 100/200/400 timing: observation only, not a durable release benchmark.

### Principal risks

The current principal limitations are unavailable headless-Chrome zoom
automation (with optional manual Firefox follow-up), the absent current 50k
static run, and disclosed environmental wall-clock flakes. The zoom follow-up
is non-blocking and not a prerequisite for release PASS. ReviewAll r2 findings
are closed and are not listed as current risks.

## Final Groups A–E closure

Groups A–E close one production tranche: canonical retrieval/API boundaries;
retained Bundle transactions, cache invalidation, conflict and event semantics;
portable/live search and Context Pack parity; workbench/graph ownership and
accessibility; and benchmark/capture proof tooling. Markdown plus YAML remains
authoritative. Derived indexes and portable corpora remain rebuildable; no DB,
model, vector service, bundler, framework, or required runtime dependency was
introduced. In this historical Groups A–E/r9 retrieval closure, Annotations v2
and proposal/diff review were explicitly deferred. Annotations v2 was implemented
only in the later follow-on tranche; proposal/diff review remains deferred.

### Surface and parity matrix

| Surface | Full Search | Context Packs | No-JS contract |
|---|---|---|---|
| CLI | Six modes; `text`/`json` only | Schema-v1 `json`/`markdown` | Not applicable |
| Live HTTP/UI | Six-mode SSR/JSON `GET /__search` | JSON/Markdown `GET /__context`; prepare/copy actions | Server GET search and canonical context links work |
| Static | Validated content-addressed corpus | Local bounded packs | Browse and prebuilt concept context only; arbitrary retrieval unavailable |
| SPA | Validated portable corpus | Local bounded packs | Interactive retrieval unavailable |
| Single file | Embedded portable corpus | Local bounded packs | Interactive retrieval unavailable |

Complete ordered portable/live search and Context Pack parity is **7/7**.
The tracked relevance report records unchanged P@3 **0.380952** and
MRR, nDCG@3, expected-section hit rate, and citation validity all **1.0**.
This is parity/evidence proof, not a ranking-improvement claim.

Retained mutations use shared locking and persist-before-publication. Dry-run
preview state is retained in memory but not written; durable apply preserves
logical per-operation attribution even when physical writes coalesce. Conflict
and persistence failure publish no stale success, while conflicts emit their
terminal event. Index/log refresh remains marker-safe/append-only and updates
retained revision and derived caches.

### Review and remediation history

The historical ReviewAll artifact
`.aic/tmp/runs/workflow/gold-release-reviewall/review.md` returned **REVISE**;
it is remediation history, not current closure. Later independent reviews passed
on their then-current tree: code review `ses_0b5604355ffeF29C7mZ4hIdOwX`,
quality-invariant review `ses_0b5604336ffe1GzWt2pVOOYNOs`, Impeccable review
`ses_0b5604371ffe0asVbipT3vtKrz`, OpenAI visual review
`ses_0b47f3e62ffeaMLl5UtXqM64Bi`, GLM visual review
`ses_0b47f3e50ffeyXr5HXzEUp9egr`, Opus visual review
`ses_0b47f3e3affejTeO4usD0GOmIq`, and ProjectUpdater documentation audit
`ses_0b3d05adfffesP0F6J2tpLR9i0`. These durable historical PASS references do
not substitute for a fresh closeout on the final frozen tree.

The current Group D independent invariant audit, session
`ses_0b1720733ffejOmSnkP0OuCzw0`, passed all eight mapped D requirements with
**37 focused cases**. The current Group E audit, session
`ses_0b172070fffetiYjWBodu1g879`, initially returned **REVISE** because the
production capture timeout and `pagehide` paths lacked direct tests and the
release matrices/history were stale. Remediation added those direct tests.
A later deterministic defect was also found and fixed: malformed or missing
single-file embedded retrieval could publish terminal unavailable and then be
overwritten by graph layout's page-global readiness. Search and graph now own
target-specific capture signals, graph publication is centralized, unavailable
search is rejected, and valid search plus live/static graph remain authorized.

The final frozen-tree ReviewAll r10 gate **PASSed with no findings** across all
27 delegated reviews; its durable record is
`.aic/tmp/runs/workflow/gold-release-final-reviewall-r10/review.md`.
The frozen release evidence records **1672 passed, 23 optional skips, and 18
warnings**, plus strict documentation validation with **0 errors, 0 warnings,
and 15 informational findings (15 info)** across the exact **74-path** staged
inventory.

The tracked ProjectUpdater maintenance gate completed **UPDATED/PASS**. It
reviewed the exact 74-path inventory in
[`gold-standard-release-files-2026.txt`](gold-standard-release-files-2026.txt),
plus `AGENTS.md`, `SKILL.md`, `README.md`, resources, canonical docs, and this
release record. Maintenance updated `README.md`,
`docs-bundle/explanation/architecture.md`,
`docs-bundle/reference/embedding_guide.md`, and this release record; it created
no archives. `scripts/okf-loom validate docs-bundle --strict` passed with **0
errors, 0 warnings, 15 info**, and the maintenance diff check passed. No
blocking gaps remained. Runtime, tests, relevance artifacts, and protected
paths were unchanged by maintenance. These tracked files, inventory, command
result, and diff check are the maintenance proof; `.aic` is not the sole proof.

### ReviewAll remediation (findings 1–23)

- **Retrieval/API:** queryless concept packs keep an internal bounded seed, so
  authored titles beyond the public query cap remain valid. Search/context
  payload and revision headers are captured atomically. Canonical JSON rejects
  unsupported and non-finite values instead of stringifying them. One absolute
  deadline covers retrieval, bounding, serialization, HTML/encoding, and final
  response caps; cancellation prevents partial cold-cache publication.
- **Transactions and authored state:** multi-index writes defer events and
  history until whole-transaction commit; index plans use expected-revision
  CAS; coalesced logical activities have honest undo ownership; failed batches
  unwind history/group artifacts. `write-concept` fails closed on malformed
  existing YAML and uses update/create CAS outside Studio. Cross-process log
  append is lock-owned.
- **Portable/workbench:** portable loading honors the stricter caller deadline;
  manifest production and embedded consumption share the 16 MiB cap. BFCache
  `pageshow` starts fresh validation. Copy ownership cannot strand another
  control; retries detach aborted provider generations. Palette listbox IDs,
  `aria-controls`, `aria-expanded`, `aria-activedescendant`, and Enter
  announcements are synchronized. Graph context requires non-empty matching
  provider/pack revisions.
- **Graph/capture:** initial fetch and refresh are page/generation owned;
  structural refresh owns a new layout generation and payload count. Empty and
  Cytoscape-degraded graphs reach honest semantic terminal states. Capture
  consumers apply one remaining 30-second budget to prepare, readiness, paint,
  and screenshot, pass abort ownership to the runtime, and preserve a prior
  valid destination on failed recapture.
- **Relevance provenance:** the regenerated tracked report binds every measured
  source through `provenance.source_manifest`, including `retrieval.py` and
  `search.py`, plus fixture, harness, measured-patch, and manifest digests.
  That pre-r2 source-manifest digest was superseded by the complete selected-
  checkout manifest recorded in the r2 closure below.
  Metrics remain truthful and unchanged; complete portable/live search and
  Context Pack parity remains 7/7.

### ReviewAll r2 remediation closure

- **Retrieval and corpus generation:** cancellation is checked during bundle
  revision hashing, every response-capping iteration, and immediately before
  passage/cache publication. Passage corpus rows and revision are captured as
  one atomic generation. CLI JSON now uses the same strict unsupported-value
  refusal as canonical HTTP JSON and emits no partial successful payload.
- **Cross-process transactions/history:** one canonical per-target
  cross-process lock owns read, expected-revision comparison, replacement, and
  rollback for Studio, non-Studio, and index writes. Rollback is conditional
  and never overwrites bytes no longer owned by its transaction. A durable
  post-content finalization envelope keeps snapshots, `mark_logged`, group
  manifests, activities, graph, and change publication coherent. Concurrent
  group-manifest updates merge under cross-process ownership without losing
  members.
- **Portable workbench:** an immediate static retry atomically detaches the
  aborted provider promise/controller/generation before starting fresh. Empty
  query/filter state preserves useful static browse content. Nested Escape is
  owned by the topmost lightbox/dialog. Copy controls clean up on close and
  pagehide; suggestions count the visible “View all” option and restart safely
  after BFCache. Single-file filter-only and zero-result Context behavior now
  matches live/static behavior.
- **Graph:** refresh validates a complete candidate and atomically replaces one
  authoritative snapshot, including totals, degree, and LOD state; Show all
  cannot resurrect deleted hidden nodes. Initial fetch/JSON/schema/init failure
  publishes a prompt stable terminal capture diagnosis for the current owner.
  Capture adapter output is strictly typed (explicit booleans, finite
  nonnegative counts/generations, matching required layout generations), and a
  queued refresh cannot start after pagehide.
- **Capture/media:** PNG/JPEG captures use unique same-directory invocation
  temporaries and only the current invocation may replace the destination.
  Phase helpers preserve an outer timer and its elapsed remaining budget.
  Canonical GIF assembly writes, verifies, and closes a unique same-filesystem
  temporary before atomic replacement, preserving the previous GIF on failure.
- **Relevance closure:** measured sources are selected-checkout relative and
  include the complete `scripts/okf_loom/**/*.py`, viewer template/static, and
  harness/fixture closure without absolute or `.aic` paths. The shared
  `--verify-report` path recomputes file hashes, aggregate/source-manifest
  digest, harness/fixture identity, tree/patch identity, metrics, and both
  parity totals and fails closed on tampering. The historical r2 source-
  manifest digest was `6fb2185401e58cc3d8b6ca34b968aaa4e6c451f925b53f7b9c9297063659a12e`
  and is superseded by r5.
  P@3 remains 0.380952; MRR, nDCG@3, expected-section hit rate, citation
  validity, and both 7/7 parity measures remain unchanged.

### ReviewAll r3 remediation closure

- **Studio transaction sidecars and groups:** transaction-wide ownership now
  covers exact transaction-created history, logged revisions, group state,
  and append-only events. Failure compensation cannot erase another process's
  sidecars. Finalization retains and publishes its exact event records, uses
  canonical dedup keys across rotation and feeds larger than 1,000 rows, and
  broadcasts each committed event exactly once. Deferred group operations
  reserve capacity before content mutation; valid concurrent members merge,
  while malformed, wrong-shape, or unreadable manifests fail closed without
  byte changes.
- **Corpus identity:** chunk identity reuses the already-produced canonical
  bytes rather than serializing again under the derived-state lock. Identity
  generation remains cancellation-aware and publishes neither corpus nor
  cache after cancellation.
- **Graph readiness:** only the authoritative owned `layoutstop` may publish
  successful layout settlement. A layout deadline stops the owned layout and
  publishes an explicit timeout terminal, never fabricated success. Capture
  adapter validation enforces settlement, coherent required/no-layout
  generations, matching nonzero generations, finite nonnegative counts, and
  `nodeCount <= payloadNodeCount` before authorization.
- **Capture/media ownership:** off-main capture is rejected before creating a
  worker or artifact, and a permanently blocked attempted phase cannot keep the
  process alive. Destination sequencing is cross-process, not only
  process-local. Recurring outer timers
  are restored with their remaining interval even after firing. README media
  capture uses invocation-private frame staging and concurrent GIF destination
  ownership; verified GIF replacement remains atomic and preserves prior
  output on failure.
- **Relevance independence:** verification material now lets the verifier
  independently reconstruct live and portable search contracts, Context Pack
  fields/item order/canonical Markdown, parity flags/totals, and citation
  validity. Citations are checked against regenerated fixture physical bytes,
  line bounds, offsets, paths, and exact excerpts rather than trusting stored
  booleans. The historical r3 selected-checkout source-manifest digest was
  `8ef668ef8896776d8d7e816d209ffa32de203c88379607efbb2a04c82a50592e`;
  and its measured patch digest was
  `2f90086b597df5eeaef3f7769bc49ebd2c997dbf471fb5e592ce79836734e748`.
  Metrics remain unchanged and ordered search/Context parity remains 7/7.
- **Portable polish:** single-file result activation now selects/focuses the
  graph concept by pointer and keyboard, suggestion announcements distinguish
  direct result count from “View all,” and copy revocation clears stale manual
  fallback content.

The approved no-JS contract is explicit: honest static browse/concept-only
fallback or explicit unavailable state. Arbitrary search and query-generated
Context Packs require JavaScript or the live server.

### ReviewAll r4 remediation closure

- **Transaction preflight and event delivery:** grouped transactions preflight
  the complete unique member set and reserve final capacity before the first
  content write. Grouped index writes validate the manifest and participate in
  the same accounting. Finalization registers canonical event keys before
  direct publication, publishes retained records exactly once, and advances a
  durable cursor across active/rotated files without a 500-row loss window.
- **Imported retrieval cancellation:** imported corpus validation applies
  declared descriptor/chunk bounds before complete serialization, canonicalizes
  once, reuses those bytes for identity, and checks cancellation throughout.
  Cold ContentIndex and graph construction release the derived-state lock and
  publish no cache when cancelled.
- **Graph terminal ownership:** non-empty→empty refresh fences prior layout and
  publishes an authoritative settled empty state with zero active generations;
  empty→non-empty starts a fresh owned layout. Adapter terminal ordering
  preserves valid layout-timeout diagnostics while contradictory pending state
  fails promptly. Every readiness terminal removes its abort listener.
- **Capture ownership:** synchronous Playwright remains on its main owning
  thread; off-main capture is rejected before work/artifact creation, and no
  permanently blocked non-daemon worker can keep the process alive. Existing
  `SIG_IGN`, subprocess-isolated `SIG_DFL`, callable one-shot, and recurring
  timer semantics are preserved. Live/static server `__enter__` failure cleans
  started sockets, threads, and processes.
- **Relevance replay:** verification independently replays the pinned baseline
  from its clean revision and rejects coherent baseline row/aggregate tampering.
  The historical r4 selected-checkout source digest was
  `ccd0315aeae27acb8335fc411e48579228558e4920ff726653474ad876ff0bb1`;
  and its measured patch was
  `d667c44b5366de967a175d6ec63aab10617bf1ec557b4182e811a879472b98c7`.
  Both r4 digests are superseded by r5. Metrics remain unchanged and ordered live/portable search and Context Pack
  parity remains 7/7.
- **Portable polish:** result activation retains a usable fallback when graph
  activation is unavailable, suggestion announcements use the rendered direct
  option cap, and revocation/close/pagehide clears stale fallback state.

The exact intended release file set is tracked in
[`gold-standard-release-files-2026.txt`](gold-standard-release-files-2026.txt).
It includes source, documentation, tests, scripts, the relevance report,
Context Pack reference, and itself; it excludes root `.gitignore`, every
`.aic/**`, `.venv/**`, bytecode/cache files, and ignored screenshots/temp proof.
The six canonical screenshots under `.aic/tmp/group-e-capture-proof/` are
supplied local **non-release** evidence. The tracked executable tests and
capture command are the durable proof; screenshot bytes are intentionally not
release files.

### ReviewAll r5 remediation closure

- **Event visibility and group capacity:** locally appended transaction events
  are tail-ineligible before direct visibility and canonical-key registration,
  preventing a tail/direct double publish. Capacity preflight uses the union of
  committed, reserved, and complete transaction members, including grouped
  index membership.
- **Imported corpus bounds:** validation maintains cumulative encoded-byte and
  work bounds incrementally across chunks and nested projections, with
  cancellation checkpoints before any monolithic materialization or partial
  return.
- **Signal ownership:** inherited `SIG_IGN` semantics are preserved for both
  one-shot and periodic schedules; an ignored outer alarm does not mask the
  capture phase's own deadline.
- **Graph atomic refresh:** refreshed body data, graph inventory, provider and
  capture revision commit as one authoritative candidate. Empty/non-empty
  transitions cannot expose a new body with an old capture revision or vice
  versa.
- **Relevance identity:** release identity is content-addressed and remains
  stable across clean post-commit replay and unmeasured metadata changes. The
  historical r5 release content digest was
  `fffe38f7ff37b6fd925cf046b28b6c4349d1be3c29d4132157b1ecb424506cc6`;
  and its source manifest was
  `cec56d22e9850895e313f6fdc202f6e151c6f8882390e37b5731d1f9e6e375dc`.
  Both r5 digests are historical and superseded by r6. Metrics remain unchanged
  and parity remains 7/7.

Capture sets produced before the current accepted set are historical and
superseded. The current accepted local set remains exactly six screenshots at
`.aic/tmp/group-e-capture-proof/2026-07-11-viewer/`; it is ignored non-release
evidence, while tracked executable tests and the capture command remain the
durable release proof.

### r6 imported-corpus aggregate/projection/index closure

Imported corpus validation now enforces one corpus-wide canonical-byte total
across all chunks, including an exact accepted boundary. Recursive public
projection validation checks cancellation before encoding, bounds object keys
by both character and UTF-8 byte length, and charges one shared projection-work
budget across the complete corpus rather than resetting per passage. Work or
byte exhaustion returns typed `corpus_too_large` with no partial result.
Chunk indexes remain strict canonical integers: mixed string, float, boolean,
or null indexes fail with `invalid_corpus` rather than being coerced.

The historical r6 relevance identity superseded r5 and earlier digests, but is
now itself superseded by r7. Its release content digest was:
`bb9d6438fdc28d8343966ac5dfd5325a7d1dfbca81b0360671fb7a3c3e444972`.
Its source manifest was:
`041abf3ed68d4fda692cf2ed358f6dec73ff54f610c53e38ec22a75e1f179c6d`.
Metrics and complete ordered 7/7 search/Context parity remain unchanged.

### r7 Unicode and cancellation closure

Canonical JSON serialization now wraps lone-surrogate encoding failures in a
stable `invalid_json_value` contract. Imported corpus validation rejects lone
surrogates in passage fields and recursive projection keys/values as
`invalid_corpus_passage`, without leaking a raw Unicode exception or returning
partial data. Recursive projection cancellation remains checked before passage
encoding, so cancellation and Unicode refusal share the same fail-closed
publication boundary.

Historical r7 release content digest:
`d5bb55b6fec7ad3b9c7ca4ee27a2886ff14bd1b9ea163683fa6605bdee228811`.
Historical r7 source manifest:
`5d7e5511aaf6c20e5c52351ddad92fbb3fd63e7d99ab54b68cc09d58ff22195f`.
Both r7 digests are superseded by r8. Metrics
and complete ordered 7/7 search/Context parity remain unchanged.

### r8 exact corpus schema/order and retained reload ownership

Imported corpus artifacts now require the exact top-level object schema; extra
or missing keys and lone-surrogate top-level keys/values fail with stable
`invalid_corpus`. Full passage ordering uses the complete canonical ordering
tuple, so distinct passages sharing an earlier prefix cannot be reversed even
when chunk hashes and identities are recomputed.

Retained `Bundle.reload()` now owns a compare-and-swap retry loop across disk
scan and publication. If a mutation publishes after scanning begins, reload
retries instead of replacing newer retained state with the stale scan. The
retry commits one coherent disk/retained generation and preserves derived-cache
ownership.

Historical r8 release digest:
`83922d2a8adbd8c36dc85ec26ef52b3ceddcbacf687af58c4dd293a4a2a535a8`.
Historical r8 source manifest:
`242ad195a8c749bfbd4f0a795b1f82047d8187e41d68a27f96c3a9fd84becb45`.
Both r8 digests are superseded by r9. Metrics and complete ordered
7/7 search/Context parity remain unchanged.

### r9 failed-rollback/reload generation ownership

`Bundle.reload()` now retries when its disk scan observes temporary bytes from
a transaction that subsequently fails and rolls back. Reload may publish only
when the retained generation still matches the scan's ownership baseline; a
rollback or concurrent mutation invalidates the candidate and forces a fresh
scan. This prevents failed transaction bytes from becoming authoritative
retained state or derived-cache input.

Exact proof:
`tests/test_state_ownership.py::test_reload_retries_after_scanning_failed_transaction_temporary_bytes`,
with adjacent generation ownership in
`tests/test_state_ownership.py::test_reload_retries_when_mutation_publishes_after_disk_scan_started`.

Current release digest:
`1e7c168be37fa7b19a0f3bd20fdf0e2b8a24a8b4b832beb5d35a87b538100230`.
Current source manifest:
`e00c27446e945ec6f9d306f8646bbb141ffe583fbdd2b6dfe0a4621c9291708e`.
All earlier digests are historical and superseded. Metrics and complete ordered
7/7 search/Context parity remain unchanged.

Focused evidence before the final target-ownership fix was **449 passed, 11
optional skips**. After that source fix, `tests/test_viewer_browser.py` passed
**107**, `tests/test_render.py` passed **74**, the capture-focused selection
passed **6**, and JavaScript lint passed. The later current focused
retrieval/render/browser/security/capture command passed **480**, with **11
optional skips** and **8 warnings**, in **203.80s** before the last external
Group C additions; broad current module runs are therefore the stronger proof
where available. Combined `tests/test_studio_iter1_browser.py` and
`tests/test_viewer_browser.py` passed **161 in 335.89s** after the revisioned
LOD fixture correction. Earlier baseline checks also passed:
strict docs validation reported 0 errors/0 warnings/15 info, JavaScript lint
passed, and Python `compileall` passed. The strict docs command was rerun after
this record was edited; its result is stated in the final gate below.

### QUALITY_RISK_CLASSIFICATION

| Applicable tag | Why it applies | Required checklist/skill and exact proof |
|---|---|---|
| `contract-runtime-parity` | CLI/live/static/SPA/single-file search and Context Pack claims cross Python, generated assets, and browser consumers. | `contract-runtime-parity` checklist; source-to-consumer rows below; parity test and report are 7/7. |
| `async-lifecycle` | Suggestions, context, providers, graph, clipboard, capture, and watcher startup can settle after timeout, supersession, or navigation. | `async-lifecycle-deadlines`; every owner/deadline/late/retry/cleanup path is mapped in `ASYNC_LIFECYCLE_MATRIX`. |
| `rendering-capture` | Capture authorization depends on semantic target state, revision, layout, visibility, and readability—not pixel change. | `rendering-capture-readiness`; `RENDERING_CAPTURE_MATRIX` plus direct production timeout/pagehide/retry tests. |
| `keyboard-focus` | Suggestion, retry, palette, and graph controls transfer active-descendant or focus ownership. | Group D audit PASS (37 cases); `test_group_c_palette_active_descendant_enter_matches_announced_option` and `test_group_d_graph_teardown_and_mobile_keyboard_focus`. |
| `state-ownership` | Search and graph capture states, providers, clipboard status, graph generations, and retained publication must not overwrite another owner. | Target-specific capture signals and ownership tests named below; retained-state tests remain in `tests/test_state_ownership.py`. |
| `fail-closed-validation` | Missing/malformed corpora, stale revisions, invalid JSON/YAML, and unavailable target state must not become ready. | Exact malformed/missing single-file and canonical validation tests below. |
| `security-boundary` | Public retrieval is bounded and production capture must reject unavailable search while preserving authorized valid targets. | Bounded request tests plus target-specific capture authorization rows below. |
| `performance honesty` | Large static work is expensive and opt-in; canvas attachment is not semantic readiness; relevance metrics did not improve. | `tests/test_benchmark_gold_standard.py`; defaults 100/1000, explicit 10k/50k static opt-ins, and unchanged tracked relevance metrics. |

### QUALITY_PROOF_MATRIX

| Acceptance/risk | Source/consumer proof | Exact executable proof |
|---|---|---|
| E1—safe benchmark defaults | `scripts/benchmark_gold_standard.py`: default scales 100/1000; large and static work require explicit flags. | `tests/test_benchmark_gold_standard.py` (6 tests). |
| E2—honest benchmark semantics | Canvas metric is attachment only (`semantic_ready:false`); no duplicate `canonical_passages` retention or unsupported memory-profile claim. | `tests/test_benchmark_gold_standard.py`; tracked relevance provenance tests. |
| E3—production capture adapter | Viewer production consumer calls stable `window.okfCaptureReadiness.start(...)`; Python capture consumers share one remaining budget. | `tests/test_viewer_browser.py::test_graph_capture_readiness_uses_production_adapter`; `tests/test_capture_consumers.py::test_prepare_hang_obeys_total_deadline_and_aborts`. |
| E4—real 30s timeout, late result, retry | Capture owner fences completion after the production deadline and a retry receives a fresh generation. | `tests/test_viewer_browser.py::test_capture_readiness_production_deadline_fences_late_settlement_and_allows_fresh_retry`; `::test_capture_readiness_timeout_late_retry_and_distinct_diagnostics`. |
| E5—navigation revocation | `pagehide` revokes the current target owner; its late result cannot authorize capture; retry is fresh. | `tests/test_viewer_browser.py::test_capture_readiness_pagehide_fences_late_settlement_and_allows_fresh_retry`. |
| E6—target-specific readiness | Search and graph signals are separate; centralized graph publication cannot overwrite unavailable search. | `tests/test_viewer_browser.py::test_graph_capture_readiness_uses_production_adapter`; `::test_single_file_nojs_and_missing_corpus_are_honest`; `::test_malformed_single_file_corpus_settles_unavailable`. |
| E7—valid portable targets remain authorized | Valid embedded search metadata and BFCache revalidation retain honest authorization; live/static graph use their graph-specific state. | `tests/test_render.py::test_single_file_exposes_offline_full_search_and_context`; `tests/test_viewer_browser.py::test_group_c_single_file_bfcache_restore_revalidates`; `::test_delayed_initial_graph_fetch_is_fenced_by_pagehide_and_bfcache_restarts`. |
| E8—capture publication/diagnostics | Consumers publish atomically, preserve a prior destination on failure, and distinguish missing/hidden/unreadable/unavailable/not-ready/timeout/revision mismatch. | `tests/test_capture_consumers.py::test_frozen_raf_obeys_deadline_and_leaves_no_temp`; `::test_failed_recapture_preserves_existing_destination`; `tests/test_viewer_browser.py::test_capture_readiness_timeout_late_retry_and_distinct_diagnostics`. |
| Group D ownership/focus gate | `wiki.js` and `graph.js` own suggestion, provider, selection, layout, and focus generations. | Independent audit `ses_0b1720733ffejOmSnkP0OuCzw0`: 37 focused cases, all eight D requirements mapped. |
| Portable/live contract parity | `retrieval.py`, `server.py`, `render.py`, generated corpus, and browser consumers share ordered search/Context Pack contracts. | `tests/test_render.py::test_browser_corpus_hash_modes_neighbors_and_context_schema`; `tests/test_benchmark_retrieval_relevance.py::test_fixture_portable_live_top_result_parity_is_complete`; parity 7/7. |
| Fail-closed and security boundary | Invalid/missing corpus and unavailable search cannot become capture-ready; public retrieval remains bounded. | Single-file tests above; `tests/test_retrieval_context.py::test_public_exports_and_aggregate_work_caps`; `::test_canonical_json_and_public_metadata_reject_non_finite_values`. |
| Responsive/accessibility and zoom honesty | Direct proof covers CSS viewport/DPR reflow and named accessibility states, not genuine browser/text zoom. Headless-Chrome zoom automation is unavailable; manual Firefox zoom is deferred and non-blocking, not a PASS prerequisite. | `tests/test_viewer_browser.py::test_search_context_reflows_at_720px_css_viewport_dpr2`; `::test_search_context_fallback_reflows_at_320px_css_viewport_dpr4`; `::test_forced_colors_preserves_search_invalid_and_copy_fallback_states`; `::test_reduced_motion_keeps_suggestion_context_selection_and_fallback_states`; relevance parity 7/7 with unchanged P@3 0.380952. |
| r2 retrieval cancellation/atomic corpus/strict CLI JSON | Hashing, capping, cache publication, corpus projection/revision, and CLI output share cancellation and strict serialization contracts. | `tests/test_retrieval_context.py::test_cancellation_during_revision_hash_does_not_publish_cache`; `::test_cancellation_during_context_capping_stops_next_iteration`; `::test_cancelled_passage_build_never_publishes_derived_cache`; `::test_passage_corpus_holds_one_generation_through_projection_and_manifest`; `::test_context_cli_json_contract_and_search_limit_validation`. |
| r2 cross-process CAS/finalization/group undo | Per-target locks and conditional rollback protect content; durable finalization and locked group merge protect history/events. | `tests/test_state_ownership.py::test_cross_process_index_create_cas_has_one_success_one_conflict`; `::test_rollback_conflict_preserves_newer_external_bytes`; `::test_post_persistence_finalization_failure_rolls_back_complete_envelope`; `::test_cross_process_group_manifest_merge_has_no_lost_members`. |
| r2 portable lifecycle/parity | Static retry, browse hydration, suggestions/BFCache, nested Escape/copy, and single-file filter Context have explicit owners. | `tests/test_viewer_browser.py::test_group_c_static_initial_browse_survives_portable_hydration`; `::test_group_c_suggestions_bfcache_restart_fences_old_result`; `::test_group_c_single_escape_copy_cleanup_and_filter_context`; `::test_static_failed_owned_reload_revokes_provider_and_retry_owns_focus`. |
| r2 authoritative graph/capture validation | Refresh atomically replaces validated snapshot/LOD state; initial failures and malformed adapters fail promptly. | `tests/test_viewer_browser.py::test_group_d_large_graph_refresh_show_all_never_resurrects_deleted_hidden_nodes`; `::test_group_d_malformed_refresh_is_atomic`; `::test_group_d_current_initial_failure_publishes_capture_terminal`; `::test_group_d_capture_adapter_shape_fails_closed`; `::test_group_d_queued_refresh_is_cancelled_on_pagehide`. |
| r2 capture invocation/GIF atomicity | Unique invocation temps, replacement authority, outer timer preservation, and verified atomic GIF publication. | `tests/test_capture_consumers.py::test_cross_process_newest_claim_fences_late_finisher`; `::test_run_phase_preserves_outer_alarm_handler_and_remaining_timer`; `::test_gif_assembly_atomically_replaces_and_leaves_no_temp`; `::test_gif_publish_failure_preserves_existing_and_cleans_only_temp`; `::test_gif_encoder_failure_preserves_existing_and_cleans_only_temp`. |
| r2 complete relevance provenance/verifier | Selected-checkout source closure and shared fail-closed `--verify-report` recomputation bind measured bytes and metrics. | `tests/test_benchmark_retrieval_relevance.py::test_manifest_uses_selected_checkout_and_complete_source_closure`; `::test_report_verifier_rejects_tampering`; `::test_tracked_report_has_sanitized_complete_provenance_and_enforced_results`; r2 digest is historical/superseded. |
| r3 transaction-wide sidecar/event/group ownership | Exact transaction records are compensated/published under sidecar ownership; capacity is reserved and invalid manifests fail closed. | `tests/test_state_ownership.py::test_failed_finalization_sidecar_lock_preserves_second_writer_state`; `::test_staged_events_are_exact_with_rotation_and_large_prior_feed`; `::test_cross_process_event_is_tailed_exactly_once`; `::test_deferred_group_capacity_is_reserved_across_processes`; `::test_unreadable_group_manifest_fails_closed`. |
| r3 cancellable corpus identity | Chunk identity reuses canonical bytes and cancellation releases ownership without publication. | `tests/test_retrieval_context.py::test_corpus_identity_cancellation_publishes_nothing_and_releases_generation_lock`. |
| r3 authoritative graph settlement/adapter invariants | Success is layoutstop-owned; timeout is terminal; contradictory adapter state cannot authorize. | `tests/test_viewer_browser.py::test_graph_layout_deadline_never_fabricates_capture_success`; strict adapter cross-field cases in `tests/test_viewer_browser.py::test_group_d_capture_adapter_shape_fails_closed`. |
| r3 managed capture/GIF ownership | Off-main rejection-before-work, blocked-process exit, cross-process destination sequence, recurring timers, private frame staging, and GIF publication retain invocation authority. | `tests/test_capture_consumers.py::test_off_main_capture_is_rejected_without_worker_or_artifact`; `::test_permanently_blocked_off_main_phase_cannot_keep_process_alive`; `::test_cross_process_newest_claim_fences_late_finisher`; `::test_run_phase_restores_periodic_outer_alarm_after_it_fires`; `::test_readme_frame_staging_is_private_per_invocation`. |
| r3 independent relevance reconstruction | Verifier reconstructs contracts, packs, Markdown, parity, and physical citations from selected-checkout fixture bytes. | `tests/test_benchmark_retrieval_relevance.py::test_verifier_reconstructs_parity_instead_of_trusting_coherent_flags`; `::test_verifier_reconstructs_citations_from_fixture_bytes`; r3 digests are historical and superseded by the r4 pinned-baseline row. |
| approved no-JS degradation | Static browse/concept fallback is honest; SPA/single-file explicitly unavailable; arbitrary retrieval requires JS/live. | `tests/test_viewer_browser.py::test_static_nojs_search_and_graph_hide_inert_primary_controls`; `::test_single_file_nojs_and_missing_corpus_are_honest`; `tests/test_render.py::test_static_nojs_context_is_canonical_and_error_links_target_real_controls`. |
| r3 portable activation/count/fallback cleanup | Single results activate graph by pointer/keyboard; suggestions announce direct count separately; revocation clears fallback. | `tests/test_viewer_browser.py::test_group_c_single_results_activate_graph_by_pointer_and_keyboard`; `::test_group_c_single_copy_revocation_clears_manual_fallback`; `::test_search_suggestions_are_complete_editable_combobox`. |
| r4 full member preflight/index accounting | Complete transaction membership is reserved before content; grouped index validates/accounts against the same manifest. | `tests/test_state_ownership.py::test_grouped_index_preflight_rejects_malformed_manifest_before_content`; `::test_deferred_group_capacity_is_reserved_across_processes`. |
| r4 exact event pre-dedup/durable cursor | Canonical keys register before direct publication; durable active/rotated cursor drains every external event exactly once. | `tests/test_state_ownership.py::test_staged_event_keys_registered_before_direct_publish_and_tail`; `::test_durable_event_cursor_drains_cross_process_burst_across_rotation`; `::test_cross_process_event_is_tailed_exactly_once`. |
| r4 imported corpus/cold-index cancellation | Imported bounds precede serialization; canonical bytes are reused; cold derived construction publishes nothing after cancel. | `tests/test_retrieval_context.py::test_corpus_loader_rejects_declared_bounds_before_passage_serialization`; `::test_corpus_loader_cancels_validation_without_partial_return`; `::test_cancelled_cold_content_index_releases_lock_without_cache_and_retries`; `::test_cancelled_cold_graph_does_not_publish_and_retry_works`. |
| r4 graph empty transitions/terminal cleanup | Empty transitions publish coherent zero-generation state; timeout diagnostics and abort listeners remain owner-correct. | `tests/test_viewer_browser.py::test_graph_refresh_empty_transition_capture_truth`; `::test_capture_readiness_removes_abort_listeners_on_every_terminal`; `::test_graph_layout_deadline_never_fabricates_capture_success`. |
| r4 main-thread capture/timer/startup cleanup | Off-main work is rejected without workers/artifacts; blocked work cannot hold exit; all signal dispositions and server startup failures cleanly preserve ownership. | `tests/test_capture_consumers.py::test_off_main_capture_is_rejected_without_worker_or_artifact`; `::test_real_playwright_page_is_rejected_from_embedded_worker_thread`; `::test_permanently_blocked_off_main_phase_cannot_keep_process_alive`; `::test_run_phase_preserves_sig_ign_disposition`; `::test_run_phase_preserves_sig_dfl_in_subprocess`; `::test_static_server_enter_failure_closes_socket_and_thread`. |
| r4 pinned-baseline replay | Verifier reconstructs baseline from the clean pinned revision rather than trusting report rows. | `tests/test_benchmark_retrieval_relevance.py::test_verifier_reconstructs_pinned_baseline_and_rejects_coherent_tamper`; r4 digests are historical/superseded by the r5 content-addressed row. |
| r4 portable fallback/count cleanup | Graph activation has fallback; announcements count rendered direct options; stale manual fallback is removed. | `tests/test_viewer_browser.py::test_group_c_single_results_activate_graph_by_pointer_and_keyboard`; `::test_group_c_suggestion_announcement_counts_rendered_direct_cap`; `::test_group_c_single_copy_revocation_clears_manual_fallback`. |
| release inventory/proof boundary | Tracked sorted inventory includes all intended changed product/docs/tests/proof files and excludes protected/local artifacts. | `docs/design/gold-standard-release-files-2026.txt`; inventory existence/sort/protected-path verification recorded below; `.aic` screenshots classified non-release. |
| r5 event visibility/group union capacity | Local events are tail-ineligible until direct publication ownership is registered; group cap uses committed ∪ reserved ∪ complete transaction members. | `tests/test_state_ownership.py::test_direct_append_is_tail_ineligible_before_direct_publish`; `::test_staged_append_is_tail_ineligible_before_key_registration`; `::test_deferred_group_capacity_is_reserved_across_processes`; `::test_grouped_index_preflight_rejects_malformed_manifest_before_content`. |
| r5 cumulative imported-corpus bounds | Cumulative encoded bytes/work are bounded incrementally and cancellably before monolithic serialization. | `tests/test_retrieval_context.py::test_corpus_loader_rejects_cumulative_chunk_bytes_before_monolithic_serialization`; `::test_corpus_loader_cancels_during_cumulative_encoding_without_partial_return`; `::test_corpus_loader_bounds_huge_nested_projection_before_encoding`. |
| r5 ignored-signal preservation | SIG_IGN one-shot/periodic schedules remain ignored while the capture deadline remains effective. | `tests/test_capture_consumers.py::test_run_phase_preserves_sig_ign_disposition`; `::test_run_phase_preserves_ignored_periodic_outer_schedule`; `::test_ignored_outer_alarm_does_not_mask_capture_deadline`. |
| r5 graph body/revision atomic refresh | Refreshed graph body, inventory, provider, and capture revision publish as one authoritative generation. | `tests/test_viewer_browser.py::test_graph_refresh_empty_transition_capture_truth`; `::test_group_d_graph_refresh_deletion_rebuilds_authoritative_state`; `::test_graph_refresh_latest_wins`. |
| r5 content-addressed relevance identity | Clean post-commit replay and unmeasured metadata preserve independently verified release identity. | `tests/test_benchmark_retrieval_relevance.py::test_release_identity_survives_clean_commit_and_unmeasured_metadata`; r5 digests are historical/superseded by r6. |
| r6 corpus-wide byte/index contract | Canonical encoded bytes accumulate across all chunks with an exact boundary; chunk indexes are strict canonical integers. | `tests/test_retrieval_context.py::test_corpus_loader_corpus_wide_bytes_reject_and_exact_boundary`; `::test_corpus_loader_malformed_mixed_chunk_indices_stay_in_contract`. |
| r6 recursive projection cancellation/key bounds | Recursive projection checks cancellation before passage encoding and bounds keys by character and UTF-8 bytes. | `tests/test_retrieval_context.py::test_corpus_loader_cancels_inside_recursive_projection_without_encoding`; `::test_corpus_loader_rejects_projection_keys_by_char_and_byte_bound`. |
| r6 shared projection-work budget | One aggregate node/work budget spans all passages and fails with no partial return. | `tests/test_retrieval_context.py::test_corpus_loader_shared_projection_work_budget_exhaustion_has_no_partial_return`. |
| r6 relevance identity | Content/source identity bound the regenerated report after corpus-contract changes. | `tests/test_benchmark_retrieval_relevance.py::test_release_identity_survives_clean_commit_and_unmeasured_metadata`; r6 digests are historical/superseded by r7. |
| r7 Unicode fail-closed contract | Lone surrogates in canonical JSON or imported passage/projection data become stable typed errors, never raw encoding failures. | `tests/test_retrieval_context.py::test_canonical_serializer_wraps_lone_surrogates`; `::test_corpus_loader_rejects_lone_surrogates_with_stable_error`. |
| r7 Unicode/cancellation publication boundary | Recursive cancellation occurs before encoding and neither cancellation nor invalid Unicode returns partial corpus state. | `tests/test_retrieval_context.py::test_corpus_loader_cancels_inside_recursive_projection_without_encoding`; `::test_corpus_loader_rejects_lone_surrogates_with_stable_error`. |
| r7 relevance identity | Regenerated report bound Unicode/cancellation runtime bytes. | `tests/test_benchmark_retrieval_relevance.py::test_release_identity_survives_clean_commit_and_unmeasured_metadata`; r7 digests are historical/superseded by r8. |
| r8 exact top-level corpus schema | Imported artifact accepts exactly the declared top-level keys and Unicode-safe structure. | `tests/test_retrieval_context.py::test_corpus_loader_requires_exact_top_level_schema_with_unicode_safe_error`. |
| r8 unique full passage ordering | Canonical order compares the complete passage tuple and rejects reversed distinct same-prefix passages. | `tests/test_retrieval_context.py::test_corpus_loader_rejects_reversed_distinct_same_prefix_passages`; `::test_corpus_loader_strict_passage_schema_identity_and_canonical_order`. |
| r8 retained reload CAS/retry ownership | Mutation published after disk scan forces reload retry; stale scan cannot replace newer retained state. | `tests/test_state_ownership.py::test_reload_retries_when_mutation_publishes_after_disk_scan_started`; `::test_retrieval_observes_old_or_new_only_while_commit_holds_lock`. |
| r8 relevance identity | Regenerated report bound exact schema/order/reload runtime bytes. | `tests/test_benchmark_retrieval_relevance.py::test_release_identity_survives_clean_commit_and_unmeasured_metadata`; r8 digests are historical/superseded by r9. |
| r9 failed-rollback reload generation | Reload discards a scan containing temporary transaction bytes when rollback changes the retained generation, then retries from authoritative disk. | `tests/test_state_ownership.py::test_reload_retries_after_scanning_failed_transaction_temporary_bytes`; `::test_reload_retries_when_mutation_publishes_after_disk_scan_started`. |
| r9 relevance identity | Current report binds failed-rollback/reload ownership runtime bytes. | `tests/test_benchmark_retrieval_relevance.py::test_release_identity_survives_clean_commit_and_unmeasured_metadata`; release `1e7c168be37fa7b19a0f3bd20fdf0e2b8a24a8b4b832beb5d35a87b538100230`; source `e00c27446e945ec6f9d306f8646bbb141ffe583fbdd2b6dfe0a4621c9291708e`. |

The **74-path inventory** recorded here is the frozen historical retrieval r9
release inventory. Its files were verified for that tranche after the stated
exclusions; it is not an inventory of today's modified/untracked release
candidates. In particular, the later Annotations v2 runtime, tests,
documentation, and nine-image capture candidates belong to the follow-on gate
described below and must not be compared against this old retrieval count.
Historical exclusions were root `.gitignore`, every `.aic/**`, `.venv/**`,
`__pycache__`, `.pyc`/`.pyo`, ignored screenshots, and temporary proof
artifacts. The 74th historical path was
`tests/test_studio_iter1_backend.py`, which proved union-based group-capacity
behavior in that release.

### ASYNC_LIFECYCLE_MATRIX

| Operation / starts when | Deadline owner / abort path | Completion/publication | Late result / retry | Cleanup | Exact proof |
|---|---|---|---|---|---|
| Suggestions: query/input starts generation | Suggestion generation; supersession/timeout/pagehide aborts owner | Active owner alone publishes options/count including visible “View all” and active descendant | Stale owner ignored; new input/BFCache restoration is fresh | Close listbox, clear timer/controller on pagehide | `tests/test_viewer_browser.py::test_suggestions_timeout_and_superseded_response_are_deterministic`; `::test_group_c_suggestions_bfcache_restart_fences_old_result`. |
| Live context: prepare/copy action | Workbench/graph action; absolute deadline and `pagehide` abort | Matching revision/fingerprint may publish pack | Late/stale response fenced; retry creates owner | Clear timer/controller/provider | `tests/test_viewer_browser.py::test_live_context_pagehide_fences_late_response`; Group D audit. |
| Static context: local provider action | Caller-capped 30s portable owner; query/filter/bound/pagehide revokes | Validated matching pack only; empty input preserves browse; single filter-only/zero result matches other surfaces | Late generation ignored; timeout atomically detaches promise/controller/provider before retry; BFCache reloads | Revoke generation/provider/controller before abort | `tests/test_viewer_browser.py::test_static_failed_owned_reload_revokes_provider_and_retry_owns_focus`; `::test_group_c_static_initial_browse_survives_portable_hydration`; `::test_group_c_single_escape_copy_cleanup_and_filter_context`. |
| Clipboard: copy begins | Owning control/generation; 5s where applicable; `pagehide` revokes | Owner publishes copied/manual fallback | Late settlement cannot overwrite newer status; explicit retry | Clear timer/generation | `tests/test_viewer_browser.py::test_clipboard_fallback_late_settlement_and_320px_capture`. |
| Static provider/corpus: load begins | Static/single provider, caller-capped 30s; abort on pagehide | Validate cap/hash/revision/order before publish | Old provider ignored; retry/cache bypass/BFCache revalidation fresh | Abort and revoke provider | `tests/test_viewer_browser.py::test_static_provider_setup_failure_publishes_explicit_error`; `::test_static_search_pagehide_revokes_late_provider_publication`; `::test_group_c_single_file_bfcache_restore_revalidates`. |
| Graph fetch/layout/revision: page or refresh begins | Page/fetch/provider/layout/debounce generations; context 30s; teardown/pagehide aborts | Fully validated candidate atomically replaces authoritative graph/LOD snapshot; initial failure publishes stable terminal capture state | Old fetch/layout/debounce ignored; malformed refresh publishes nothing; refresh/BFCache creates generations | Central teardown clears fetch/provider/selection/layout/fallback/debounce | `tests/test_viewer_browser.py::test_delayed_initial_graph_fetch_is_fenced_by_pagehide_and_bfcache_restarts`; `::test_group_d_graph_refresh_deletion_rebuilds_authoritative_state`; `::test_group_d_large_graph_refresh_show_all_never_resurrects_deleted_hidden_nodes`; `::test_group_d_queued_refresh_is_cancelled_on_pagehide`; `::test_graph_refresh_latest_wins`. |
| Studio sidecar/event/group finalization: content transaction begins | Per-target plus transaction-wide sidecar/group owners; capacity reservation precedes content | Exact snapshots/logged revisions/group members/events finalize as one owned envelope; exact records publish once | Failure compensates only transaction-owned records; concurrent writer survives; retry reacquires capacity/ownership | Release reservations/locks; canonical event dedup across rotation/tail | `tests/test_state_ownership.py::test_failed_finalization_sidecar_lock_preserves_second_writer_state`; `::test_staged_events_are_exact_with_rotation_and_large_prior_feed`; `::test_cross_process_event_is_tailed_exactly_once`; `::test_deferred_group_capacity_is_reserved_across_processes`. |
| Transaction/event preflight and tail: grouped transaction or tail pass begins | Transaction-wide group reservation and durable event cursor own membership/publication | Complete unique members preflight before content; canonical event keys pre-register; cursor drains active/rotated backlog | Rejected capacity/invalid manifest writes nothing; direct/tail race dedups; bursts are not dropped | Release reservation; persist cursor after consumed exact records | `tests/test_state_ownership.py::test_grouped_index_preflight_rejects_malformed_manifest_before_content`; `::test_staged_event_keys_registered_before_direct_publish_and_tail`; `::test_durable_event_cursor_drains_cross_process_burst_across_rotation`. |
| Local event visibility/group-union preflight: append or transaction begins | Direct publisher owns a tail-ineligible event until canonical registration; group lock owns committed/reserved/transaction union | Event becomes tail-eligible only after direct visibility ownership; complete union passes cap before writes | Tail cannot race unpublished local event; rejected union publishes no content/member | Clear ineligible marker after canonical key registration/publication | `tests/test_state_ownership.py::test_direct_append_is_tail_ineligible_before_direct_publish`; `::test_staged_append_is_tail_ineligible_before_key_registration`; `::test_deferred_group_capacity_is_reserved_across_processes`. |
| Corpus identity: atomic passage generation begins | Retrieval generation/cancellation event owns canonical bytes and identity | Identity derives from already-produced bytes; cache/corpus publish only after final cancellation check | Cancelled generation publishes nothing; retry rebuilds under fresh owner | Release derived-state lock without duplicate serialization | `tests/test_retrieval_context.py::test_corpus_identity_cancellation_publishes_nothing_and_releases_generation_lock`. |
| Imported corpus/cold derived index: validation or cold search begins | Retrieval cancellation event owns bounds, validation, ContentIndex/graph construction, and lock | Bounds checked before serialization; one canonical byte sequence feeds identity; cache publishes only after final current check | Cancelled validation returns no partial corpus; cancelled cold build publishes no cache; retry is fresh | Prompt lock release and temporary projection cleanup | `tests/test_retrieval_context.py::test_corpus_loader_rejects_declared_bounds_before_passage_serialization`; `::test_corpus_loader_cancels_validation_without_partial_return`; `::test_cancelled_cold_content_index_releases_lock_without_cache_and_retries`; `::test_cancelled_cold_graph_does_not_publish_and_retry_works`. |
| Incremental imported corpus accounting: each descriptor/chunk/nested value enters validation | Corpus validator owns cumulative encoded-byte/work counters and cancellation | Bounds are charged incrementally before complete projection/serialization | Over-budget/cancelled input returns no partial corpus; retry starts counters fresh | Drop partial encoding/projection and release owner | `tests/test_retrieval_context.py::test_corpus_loader_rejects_cumulative_chunk_bytes_before_monolithic_serialization`; `::test_corpus_loader_cancels_during_cumulative_encoding_without_partial_return`; `::test_corpus_loader_bounds_huge_nested_projection_before_encoding`. |
| Corpus-wide projection/index validation: chunks and recursive public metadata enter validation | One corpus owner holds canonical-byte and projection-node counters; cancellation propagates recursively | Exact byte boundary may publish only after strict integer index/order and key bounds pass | Mixed/noncanonical index, oversized key, exhausted shared work, or cancellation returns no partial corpus | Discard recursive projection and counters; retry starts a fresh owner | `tests/test_retrieval_context.py::test_corpus_loader_corpus_wide_bytes_reject_and_exact_boundary`; `::test_corpus_loader_malformed_mixed_chunk_indices_stay_in_contract`; `::test_corpus_loader_cancels_inside_recursive_projection_without_encoding`; `::test_corpus_loader_shared_projection_work_budget_exhaustion_has_no_partial_return`. |
| Unicode/cancellation during imported projection: recursive field/key/value validation begins | Corpus owner propagates cancellation and strict UTF-8 serialization errors | Valid Unicode only may reach passage encoding/publication | Lone surrogate returns typed invalid value/passage; cancellation returns timeout; neither publishes partial state | Discard recursive projection/encoded bytes and release owner | `tests/test_retrieval_context.py::test_canonical_serializer_wraps_lone_surrogates`; `::test_corpus_loader_rejects_lone_surrogates_with_stable_error`; `::test_corpus_loader_cancels_inside_recursive_projection_without_encoding`. |
| Exact corpus schema/order validation: imported artifact enters loader | Corpus validation owner covers top-level shape, complete passage tuple, chunk identity, and publication | Only exact schema and globally canonical unique ordering may publish | Extra/missing/invalid-Unicode top-level fields or reversed same-prefix passages fail with no partial corpus | Drop candidate artifact and release validation owner | `tests/test_retrieval_context.py::test_corpus_loader_requires_exact_top_level_schema_with_unicode_safe_error`; `::test_corpus_loader_rejects_reversed_distinct_same_prefix_passages`; `::test_corpus_loader_strict_passage_schema_identity_and_canonical_order`. |
| Retained reload scan/publication: `Bundle.reload()` begins disk scan | Bundle CAS generation owns scan and final retained publication | Scan publishes only if retained generation is unchanged | Concurrent mutation after scan start invalidates candidate; reload retries from disk and never overwrites newer state | Discard stale candidate; retry under fresh generation; derived caches follow winner | `tests/test_state_ownership.py::test_reload_retries_when_mutation_publishes_after_disk_scan_started`; `::test_retrieval_observes_old_or_new_only_while_commit_holds_lock`. |
| Failed transaction rollback during retained reload: scan has observed temporary persisted bytes | Reload candidate generation is compared against transaction/rollback retained generation before publication | Candidate publishes only if no rollback or mutation changed ownership | Failed transaction rollback invalidates temporary-byte scan; reload discards it and retries authoritative disk | Drop candidate and derived caches; retry under fresh generation | `tests/test_state_ownership.py::test_reload_retries_after_scanning_failed_transaction_temporary_bytes`; `::test_reload_retries_when_mutation_publishes_after_disk_scan_started`. |
| Graph layout settlement: owned layout starts | Graph layout generation and deadline own layoutstop/stop | Only current authoritative `layoutstop` publishes success; deadline publishes timeout terminal | Late layoutstop ignored; retry/refresh starts new generation | Stop layout, clear timer/listener | `tests/test_viewer_browser.py::test_graph_layout_deadline_never_fabricates_capture_success`. |
| Empty graph transition/readiness terminal: refresh or readiness starts | Authoritative graph generation and target readiness owner | Empty transition fences prior layout and publishes settled zero-generation adapter; non-empty transition owns fresh layout | Prior layout/adapter cannot authorize; timeout/unavailable terminal is preserved | Remove abort listener on every terminal and clear prior layout listeners | `tests/test_viewer_browser.py::test_graph_refresh_empty_transition_capture_truth`; `::test_capture_readiness_removes_abort_listeners_on_every_terminal`. |
| Graph body/capture revision refresh: validated candidate commits | One authoritative graph refresh owner covers body maps, inventory, provider, revision, layout/capture signal | All refreshed fields become visible atomically | Stale/partial candidate publishes nothing; capture cannot observe mixed body/revision | Dispose prior generation after commit; clean candidate on failure | `tests/test_viewer_browser.py::test_graph_refresh_empty_transition_capture_truth`; `::test_group_d_graph_refresh_deletion_rebuilds_authoritative_state`; `::test_graph_refresh_latest_wins`. |
| Production capture: `start(target)` | Target-specific capture owner, real 30s deadline; abort/pagehide revoke | Current search or graph signal may authorize after semantic checks and paint boundary | Timeout/pagehide late settlement fenced; fresh retry gets owner | Clear timer/abort/listeners | `tests/test_viewer_browser.py::test_capture_readiness_production_deadline_fences_late_settlement_and_allows_fresh_retry`; `::test_capture_readiness_pagehide_fences_late_settlement_and_allows_fresh_retry`; `::test_capture_readiness_timeout_late_retry_and_distinct_diagnostics`. |
| Single-file readiness: embedded load begins | Search provider and graph layout own distinct signals | Missing/malformed search stays unavailable; valid search and graph may independently authorize | Late/replaced owner cannot overwrite another target; BFCache revalidates | Revoke provider and target owner | `tests/test_viewer_browser.py::test_single_file_nojs_and_missing_corpus_are_honest`; `::test_malformed_single_file_corpus_settles_unavailable`; `::test_graph_capture_readiness_uses_production_adapter`; `::test_group_c_single_file_bfcache_restore_revalidates`. |
| Capture consumer/media: navigation or GIF assembly begins | Python consumer owns one 30s navigation→prepare→readiness→paint→screenshot budget and preserves outer timer authority | Unique invocation temp verifies, closes, and atomically replaces only while current; GIF follows same envelope | Late/concurrent process cannot publish over the newest claim; rerun is fresh; prior artifact survives failure | Remove invocation-owned temp only; preserve old destination and outer alarm | `tests/test_capture_consumers.py::test_cross_process_newest_claim_fences_late_finisher`; `::test_run_phase_preserves_outer_alarm_handler_and_remaining_timer`; `::test_gif_assembly_atomically_replaces_and_leaves_no_temp`; `::test_gif_publish_failure_preserves_existing_and_cleans_only_temp`. |
| Off-main capture/GIF invocation: embedded caller or README capture begins | Main-thread Playwright ownership plus cross-process destination sequence; recurring outer timer remains caller-owned | Off-main capture refuses before worker/artifact creation; current process claim alone may publish verified output | Rejected off-main work cannot leak a worker; permanently blocked attempted phase cannot hold process exit; older process claim cannot win | No off-main temp/worker; delete private staging only | `tests/test_capture_consumers.py::test_off_main_capture_is_rejected_without_worker_or_artifact`; `::test_real_playwright_page_is_rejected_from_embedded_worker_thread`; `::test_permanently_blocked_off_main_phase_cannot_keep_process_alive`; `::test_cross_process_newest_claim_fences_late_finisher`; `::test_readme_frame_staging_is_private_per_invocation`. |
| Main-thread capture/server startup: capture or context manager entry begins | Synchronous Playwright owner thread and server context own lifecycle; inherited signal disposition remains caller-owned | Off-main call refuses before work; server publishes only after complete startup | No detached worker/process blocks exit; failed startup closes partial resources; retry starts clean | Preserve SIG_IGN/SIG_DFL/timers; close socket/thread/process on enter failure | `tests/test_capture_consumers.py::test_off_main_capture_is_rejected_without_worker_or_artifact`; `::test_permanently_blocked_off_main_phase_cannot_keep_process_alive`; `::test_run_phase_preserves_sig_ign_disposition`; `::test_run_phase_preserves_sig_dfl_in_subprocess`; `::test_static_server_enter_failure_closes_socket_and_thread`. |
| Ignored outer alarm: capture phase begins under SIG_IGN one-shot/periodic schedule | Caller retains SIG_IGN schedule; phase owns only its capture deadline | Ignored alarm remains ignored and recurring schedule is restored; phase deadline still terminates work | Outer alarm cannot become timeout or mask phase timeout | Restore disposition plus remaining interval/period | `tests/test_capture_consumers.py::test_run_phase_preserves_sig_ign_disposition`; `::test_run_phase_preserves_ignored_periodic_outer_schedule`; `::test_ignored_outer_alarm_does_not_mask_capture_deadline`. |
| Watch startup: invocation begins | CLI caller supplies explicit `--since` anchor; watcher cursor owns subsequent events | Only post-anchor comment/change completes wait | Startup event cannot be missed or spuriously consumed; rerun uses new anchor | Watch process exits/cleans normally | Focused wait gate 20/20 and watch-file gate 9/9; race fix is test-harness-only. |

### RENDERING_CAPTURE_MATRIX

| Target | State owner and readiness | Fail-closed diagnostics | Production consumer/parity proof | Limitation |
|---|---|---|---|---|
| Search/workbench | Search provider/corpus generation; matching valid metadata and revision | Missing, malformed, unavailable, timeout, superseded, revision mismatch | `test_single_file_nojs_and_missing_corpus_are_honest`; `test_malformed_single_file_corpus_settles_unavailable`; valid single-file render and BFCache tests | Static no-JS cannot perform arbitrary retrieval. |
| Live graph | Graph page/fetch/layout generation; non-empty semantic inventory and applied current layout | Missing, hidden, unreadable, network, unavailable, not-ready, timeout, revision mismatch | `test_graph_capture_readiness_uses_production_adapter`; `test_delayed_initial_graph_fetch_is_fenced_by_pagehide_and_bfcache_restarts` | Empty/degraded graphs may settle honestly without claiming a drawable semantic graph. |
| Static/single-file graph | Graph-specific signal, independent of search availability | Same stable graph diagnostics; cannot overwrite search state | production-adapter test plus missing/malformed/valid single-file tests | Canvas attachment metric is not semantic readiness. |
| PNG/JPEG artifact | Python consumer's remaining total budget and atomic destination owner | Nonzero failure, no new artifact; previous destination preserved | `tests/test_capture_consumers.py` timeout, frozen-rAF, screenshot-hang, and failed-recapture tests | Local ignored screenshots are supplemental, not release authority. |

Automated tests cover CSS viewport/DPR reflow at 720/DPR2 and 320/DPR4; that
is responsive-layout proof, **not** browser page zoom or text-only zoom proof.
Genuine zoom automation is unavailable in the current headless Chrome
environment. Manual Firefox zoom remains a disclosed deferred, non-blocking
follow-up and is not required for release PASS. Ignored manual/blocker
artifacts are supplemental; no 200%/400% browser/text-zoom PASS is claimed.

### Group D ownership and accessibility closure

The parent focused gate passed **310** tests. Impeccable, quality-invariant,
and code reviews passed. Current OpenAI and Opus visual reviews reported no
P0/P1; no further vision-glm run was performed per instruction. Local Group D
matrices and six screenshots are ignored evidence under
`.aic/tmp/frontend-group-d-proof/`, not tracked executable proof.

Automated accessibility tests prove CSS viewport/DPR reflow at **720/DPR2**
and **320/DPR4**. They do **not** prove 200%/400% page or text zoom. In a
concrete system Chrome 145.0.7632.159 headless experiment, `Ctrl++` and
`Ctrl+0` were no-ops: DPR, `innerWidth`, `visualViewport`, scale, screen width,
and resolution query did not change. Genuine page-zoom automation is blocked
in this environment; text-only zoom remains manual/unavailable. Manual Firefox
zoom evidence is a deferred, non-blocking follow-up, not a prerequisite for
this release PASS. No genuine zoom proof is claimed.

### Group E benchmark and production capture closure

Benchmark defaults are exactly 100 and 1,000 concepts. Any size above 1,000
requires `--large-scales`; static measurement requires `--static-build`; large
static requires both. `browser_graph_mount` remains a compatibility key but
measures **canvas attachment only** and emits `semantic_ready: false`. No
duplicate-corpus optimization was made because current source has no cited
duplicate representation and no current profile demonstrates one. No new
10k/50k run was started.

Current scale facts are intentionally not comparisons: the current 10k static
measurement is **358,417.871 ms**, **143,736,917 bytes**, and **20,058 files**;
the current 10k canvas-attachment metric is **2,018.740 ms** and is not semantic
readiness. Current 50k static was not started because the baseline exceeded
2.5 hours. Thresholds and retries were not relaxed. The LOD synthetic fixture
now supplies the required `bundle_revision:'synthetic-500'`, preserving strict
graph payload validation rather than weakening production schema checks. Five
focused runs eliminated the deterministic graph-schema failure, but **3/5**
exceeded the unchanged historical `<2s` budget (**2.76/4.01/4.85s**); the
combined browser modules nevertheless passed 161/161. This remains known
pre-existing/environment-sensitive timing proof, not a product/schema
regression and not a uniformly green timing result. Comparative navigation
evidence likewise classifies the separate graph timeout as a known
pre-existing flake: current **1/20** versus baseline **2/20**; SSE was current
**0/20** and baseline **0/20**. In the earlier continuation graph navigation
timed out before assertions, then its exact isolated test passed in **1.70s**. This product/navigation flake
is separate from two diagnosed test-harness races fixed only in tests: the
suggestion browser interceptor installation race, and wait startup's missing
cursor anchor (now an explicit `--since` anchor; focused 20/20 and watch-file
9/9).

Production capture uses `window.okfCaptureReadiness.start(...)` as the stable
readiness owner. It checks target visibility/readability, current revision,
semantic graph inventory, and the applied layout generation within one 30s
budget; stable diagnostics distinguish missing, hidden, unreadable,
unavailable, network, not-ready, timeout, superseded, and revision mismatch.
All consumers share Chrome resolution (`--chrome`, `OKF_CHROME`,
`AIC_PLAYWRIGHT_CHROME_PATH`, system paths, Chrome channel, managed
Playwright) and atomically publish MIME-correct PNG/JPEG files.

The original Group E focused gate passed 297 tests. Later remediation gates
included **449 passed/11 optional skips** before the final target-ownership
fix, then browser **107**, render **74**, capture-focused **6**, and lint PASS
after that fix. The accepted ignored
production proof at `.aic/tmp/group-e-capture-proof/2026-07-11-viewer/`
contains exactly six files:
`live-graph.png`, `live-concept-orders.png`, `live-search.png`,
`static-graph.png`, `static-concept-orders.png`, and `static-search.png`.
These local images supplement, but do not replace, tracked executable tests.

### Compatibility, current limitations, and final gate

No bundle migration is required. Existing OKF v0.1 files, unknown
frontmatter, hand-authored prose, sessions, comments, events, direct mutators,
and undo remain readable. Search JSON additions and Context Pack schema v1 are
additive; stricter malformed-input rejection may require callers to correct
requests. Generated corpus/static assets remain derived state.

Current release limitations are narrower after r9 remediation:

- genuine browser page zoom and text-only zoom automation is unavailable in
  this headless Chrome environment; CSS viewport/DPR reflow remains the
  accurately named responsive proof, while manual Firefox zoom is a deferred,
  non-blocking follow-up and not a release-PASS prerequisite;
- no current 50,000-concept static run was started because the historical
  baseline exceeded 2.5 hours; and
- the disclosed graph LOD/CDN wall-clock and isolated navigation/SSE
  host-contention failures remain classified as environmental flakes. No
  threshold, retry count, or product behavior was relaxed.

For the historical r9 retrieval release, Annotations v2 and proposal/diff review
were deferred scope, not r9 defects. Current status differs: Annotations v2 is
implemented by its dedicated follow-on tranche, while Proposal/Diff Review and
broad performance work remain deferred. This does not retroactively add
Annotations v2 to the earlier r9 tranche.

Historical 1457/1511/1543/1590/1618/1639/1650/1659/1665-era suite totals are
retained only as history and are not post-r9 proof. The final post-r9 frozen
full suite passed with **1672 passed, 23 optional skips, 18 warnings in
349.67s**. The warnings are Python 3.12 multiprocessing `fork` deprecations.
The gate also corrected only two test-harness waits: a CSP-safe callable
presence predicate and web-first `to_be_focused()` for async focus transfer;
runtime semantics were unchanged. The regenerated relevance report retains
7/7 parity and unchanged ranking metrics without an improvement claim. No
production weakening or timeout relaxation was used.
After this documentation edit,
`scripts/okf-loom validate docs-bundle --strict` must remain at **0 errors, 0
warnings, and 15 info**.

### Annotations v2 follow-on release synchronization

Annotations v2 is current implemented behavior, not part of the historical r9
retrieval scope. Its canonical contract and exact final proof matrices are in
[`docs-bundle/reference/spec.md` §12.1](../../docs-bundle/reference/spec.md#121-annotations-v2-contract-normative-implemented).
  A pre-r4 review reported PASS after r3 remediation, but ReviewAll r4, r5, r6, r7, r8, r9, and r10
  subsequently reported FAIL on newer bytes. That PASS is historical only.
  Current closure remains pending a fresh successful ReviewAll; test results and
  exact matrix nodes do not independently establish PASS.

#### Annotations v2 ReviewAll r3 follow-on proof

This follow-on closes ReviewAll r3 without retroactively changing retrieval r9.
Canonical block IR v1 is the sole extractor/renderer/projection grammar;
renderer-owned pre-plugin capabilities receive exact post-plugin finalization;
entity-encoded unsafe URL schemes fail closed; and `block_ir_version:1` is
additive metadata while resolver v2 remains commutative.

Latest-row checkpoints, transaction-result indexes, prepared/finalized replay,
WAL hard caps, tombstone filters, notification receipts, and bounded sidecar
retirement preserve comments and idempotent outcomes across rotation. Creation
resolves the effective parent concept, validates existence, and commits under
the global concept → key → comment → directives lock order; finalized replay is
checked before current-resource access.

Browser closure adds navigation-epoch/current-concept fencing,
`concept_content_rev` reconnect identity, backoff inside the absolute deadline,
atomic failed-conflict-refresh preservation, Save focus, a ResizeObserver-owned
responsive banner, a named editor group, and Enter/Space Retry. Exact prose
selection is scoped to native browser/assistive-technology selection; Studio
controls remain keyboard proven.

The artifact contract records actual viewport/media per image and complete
path/type/tracked/mode/hash/symlink inventory entries. Injected conflict/failure
envelopes are classified separately from production-server proof, no-JS checks
use unique persisted sentinels, and one production validator runs normally and
under `python -O`. Existing bytes remain stale until regeneration; once the
manifest gate passes against the final serialized tree, the complete nine-file
set is release proof without another documentation edit.

ReviewAll r2 remediation now includes globally monotonic rotation generations
whose retention order remains correct across wall-clock regression, exact
canonical block mapping through a shared Markdown block IR, one body-plugin
seam before initial/live/static projection, canonical-visible text and duplicate
occurrence parity, one document commit/invalidation path, mandatory
`Response.ok` preservation, and one absolute 10-second GET or 15-second POST
deadline across retries. The release contract also requires explicit capture
failures, the exact nine-scenario artifact inventory, semantic mobile and
forced-colors readiness, and direct no-JavaScript and keyboard proof.

Earlier draft text called the next accumulated closure “ReviewAll r4”; that
wording did not supersede the r4/r5 FAIL artifacts. Remediation closes create append-before-notification
recovery, immediate create→re-anchor replay, unknown extension preservation,
mandatory Host and strict archive-boolean boundaries, current-byte initial/live
rendering, canonical ready hashes, shared Markdown grammar for continuation and
nested lists, task lists, indented code, multiline HTML, footnotes, escaped
table pipes, and setext headings. Browser proof now covers real A→B→A
navigation, supersession before backoff, initial-ready seeding, patch-triggered
canonical refetch, pending-v2-mark reapplication, and specialized SSE events.
Capture closure requires detailed inventory validation, per-image
classification, persisted no-JS sentinels, and atomic staging of the complete
proof directory.

Post-r5 remediation adds durable notification identities for keyed and keyless
creates/replies, canonical recovered payloads, delivery reconciliation before
WAL retirement, closed create/update schemas, current ready inventory and
polling parity, canonical initial-ready reconciliation, occurrence-qualified
draft marks, exclusive conflict-modal keyboard ownership, and a production
suggestions consumer. Projection strips valueless attributes, validates owner
tags, and maps nested siblings exactly. Capture binds mode-aware detailed
inventory/classifications, persists real sentinels, fails closed on special
files and Git errors, checks staged distinctness, and uses a recoverable atomic
directory swap. These behaviors await fresh ReviewAll; no current PASS is
claimed.

Post-r6 remediation serializes capture recovery/publication and uses atomic
exchange where available, binds tracked stage-0 Git object IDs, and aligns the
mobile manifest assertion with the captured document-peek state. Notification
receipts own immutable full payloads, including full re-anchor normal/recovery
parity. Public HTTP comments cannot forge agent authorship; reopening is limited
to dismissed rows. Browser proof now covers typed polling resync, rendered-rev
initial-ready reconciliation, post-fetch Range validation, mandatory draft
revision equality, conflict failure/supersession, overlapping modal settlement,
non-modal wide rail semantics, failed-patch fences, and BFCache cleanup. The
all-owner projection proof checks every owner. These bytes still await fresh
ReviewAll; no current PASS is claimed.

Post-r7 remediation makes atomic exchange mandatory for release publication and
holds startup recovery under the same publication lock; rollback content remains
authoritative through exchange fsync failure. The canonical mobile artifact now
drives and asserts the actual document-peek/return flow. Notification receipts
are strict immutable full payloads reserved before directive visibility, and
lifecycle/re-anchor events include their newly appended row token. Public legacy
text is bounded. Frontend proofs cover cursor-gap non-commit, full polling
envelopes, composer revision restoration, transactional patch rollback, and
latest-payload resync draining. Projection fails closed for HTML5 repair and any
pre-existing forged key, with exact deterministic tests. Unmanifested
`docs/screenshots/r6-frontend-proof/` pixels are supplemental only and are not
canonical proof. Closure still awaits fresh ReviewAll; no current PASS is claimed.

Post-r8 remediation adds bounded keyless notification checkpoints, independently
owned `claimed_at` with visible quarantine, SSE preflight/query parity, and
deadline-owned single-flight polling whose BFCache restoration awaits canonical
resync. Document/comments/events and inline reply results publish only as one
validated ownership tuple. Capture recovery accepts only complete real proof
directories, uses no-follow descriptor hashing with identity checks, deep-freezes
provenance, strictly validates Git framing/path membership, and excludes transient
lock files. Blockquote visible text recursively matches renderer grammar, while
owner/key leak guards fail closed without false positives. Closure remains
pending fresh ReviewAll; no current PASS is claimed.

Post-r9 remediation makes bounded keyless notification authority
no-false-negative across cap eviction, event pruning, and restart, and operates
receipt files through a trusted no-follow directory descriptor. Polling matches
SSE for intervals above 10,000. BFCache resync completes before reconnect;
poll/resync generations and document/comment/event, re-anchor, and inline-reply
tuples publish all-or-nothing. Capture preflights all recovery states, securely
opens intermediate components and artifacts, and derives capture-script hash
from inventory authority. Entity-decoded capability material is detected and
scrubbed, while recursive blockquote parity retains exact mappings. Closure
remains pending fresh ReviewAll; no current PASS is claimed.

Post-r10 remediation fails closed on legacy incomplete checkpoints, pins the
session/receipt directory chain, cleans constrained receipt temporaries, and
uses subscribe-first/high-water SSE handoff. BFCache reconnect waits for queued
resync completion; canonical tuples, re-anchor, conflict refresh, and inline
replies validate complete identities before any state mutation. Capture pins
output directories, inventory membership, recovery objects, artifacts, symlink
targets, and authoritative script hashes through use. Projection capability
canonicalization/scrubbing handles malformed, named, percent, and multiple
encodings under a hard bound. Closure remains pending fresh ReviewAll; no current
PASS is claimed.

Post-r11 remediation retains one trusted receipt dirfd across enumeration,
consumption, reconciliation, and cleanup. BFCache supersession preserves every
waiter until the latest queued resync is terminal. Canonical state uses
discriminated anchor/event validation and validates the complete re-anchor tuple.
Capture recovery quarantines a pinned candidate, revalidates identity and content
immediately before publication, and preserves the sole prior proof.
Canonicalization reports convergence versus exhaustion; scrub follows the same
bound and returned HTML is capability-free. Mandatory exact-source proofs cover
CRLF offsets and an explicit two-level blockquote. Closure remains pending fresh
ReviewAll; no current PASS is claimed.

The nine required final-gate images are `resolved.png`,
`stale-after-mutation.png`, `selecting.png`, `conflict.png`,
`save-failure.png`, `focus-visible.png`, `mobile-320.png`,
`forced-colors.png`, and `no-js.png`. Existing screenshot and manifest bytes
are not current-tree evidence: the parent release lane must regenerate them
after the final serialized tree settles and pass
`tests/test_annotations_capture_manifest.py`. Proposal/Diff Review and broad
performance work remain deferred and are not Annotations v2 claims.

## Factual sources

Tracked product and proof sources:

- [README](../../README.md)
- [Canonical OKF specification](../../docs-bundle/reference/spec.md)
- [Search modes](../../docs-bundle/reference/search_modes.md)
- [HTTP routes](../../docs-bundle/reference/http_routes.md)
- [Runtime architecture](../../docs-bundle/reference/architecture.md)
- [`retrieval.py`](../../scripts/okf_loom/retrieval.py)
- [`cli.py`](../../scripts/okf_loom/cli.py)
- [`model.py`](../../scripts/okf_loom/model.py)
- [`search.py`](../../scripts/okf_loom/search.py)
- [`server.py`](../../scripts/okf_loom/server.py)
- [`render.py`](../../scripts/okf_loom/render.py)
- [`test_retrieval_context.py`](../../tests/test_retrieval_context.py)
- [`test_render.py`](../../tests/test_render.py)
- [`test_viewer_browser.py`](../../tests/test_viewer_browser.py)
- [`benchmark_gold_standard.py`](../../scripts/benchmark_gold_standard.py)

Local release evidence, not shipped product artifacts:

- `.aic/tmp/sessions/aic-cx-okf-loom-v2-20260709235841/opencode/gold-benchmark/baseline-100.json`
- `.aic/tmp/sessions/aic-cx-okf-loom-v2-20260709235841/opencode/gold-benchmark/baseline-1000.json`
- `.aic/tmp/sessions/aic-cx-okf-loom-v2-20260709235841/opencode/gold-benchmark/baseline-10000-core.json`
- `.aic/tmp/sessions/aic-cx-okf-loom-v2-20260709235841/opencode/gold-benchmark/baseline-50000-core.json`
- `.aic/tmp/sessions/aic-cx-okf-loom-v2-20260709235841/opencode/gold-benchmark/static-50000.json`
- `.aic/tmp/sessions/aic-cx-okf-loom-v2-20260709235841/opencode/postfix-benchmark-report.json`
- `.aic/tmp/frontend-remediation-proof/final-screenshots/`
- job `okf-loom-gold-standard-release-3eed85` memories named in the release contract and documentation audit.
