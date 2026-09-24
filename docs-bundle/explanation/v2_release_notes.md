---
type: Explanation
title: "What changed in okf-loom v2"
description: An honest, one-page summary of how okf-loom grew from its original Markdown/OKF brief to the live Studio, full six-mode search and deterministic Context Packs, and Annotations v2 — with an old-vs-new comparison, feature inventory, demo links, current proof status, and explicitly deferred work.
resource: /explanation/v2_release_notes.md
tags: [release-notes, changelog, evolution, v2, annotations-v2, context-packs, search, studio]
timestamp: "2026-07-12T00:00:00Z"
aliases: [v2 release notes, what is new in okf-loom, release summary, evolution of okf-loom, what changed in v2]
entities:
  - label: Annotations v2
    kind: capability
  - label: Candidate-set resolver
    kind: resolver
  - label: Anchor health
    kind: contract
  - label: Explicit Re-anchor
    kind: route
  - label: Six-mode search
    kind: capability
  - label: Agent Context Packs
    kind: capability
  - label: Live Studio
    kind: viewer
  - label: Original requirements brief
    kind: source
relations:
  - type: summarizes
    target: /reference/spec.md
  - type: references
    target: /explanation/requirements.md
  - type: references
    target: /reference/search_modes.md
  - type: references
    target: /reference/context_packs.md
  - type: references
    target: /reference/comment_lifecycle.md
  - type: explained_by
    target: /explanation/live_studio_design.md
  - type: see_also
    target: /demo/annotations_v2_showcase.md
  - type: references
    target: /demo/showcase.md
  - type: follows_up_with
    target: /reference/proposal_diff_review_brief.md
provenance:
  - source: /explanation/requirements.md
    note: The verbatim original user brief (2026-06-27) and the traceability matrix that map each requirement to the artifact that delivers it.
  - source: /reference/spec.md
    note: The current canonical specification; section numbers cited below are stable anchors into this file.
  - source: /explanation/v2_release_notes.md
    note: Dated release-record entries are summarized from the bundle-root append-only update log (log.md, a reserved filename, so referenced here as a literal path rather than a concept link).
citations:
  - id: spec-overview
    text: "okf-loom spec §1–§6 — distribution model, bundle contract, six-mode retrieval. /reference/spec.md"
  - id: spec-search
    text: "okf-loom spec §6 — Search modes and zero-dependency retrieval (six modes, no embeddings shipped). /reference/spec.md#6-search-modes-and-zero-dependency-retrieval"
  - id: spec-studio
    text: "okf-loom spec §10 — Live collaborative studio model. /reference/spec.md#10-live-collaborative-studio-model"
  - id: spec-annotations-v2
    text: "okf-loom spec §12.1 — Annotations v2 contract (normative, implemented): candidate-set resolver, closed v2 anchor schema, row ownership, projection/SSE/cache contract. /reference/spec.md#121-annotations-v2-contract-normative-implemented"
  - id: context-packs
    text: "Agent Context Packs — schema-v1 deterministic, citation-ready context packs. /reference/context_packs.md"
  - id: search-modes
    text: "Search modes reference — lexical / tag / semantic / hybrid / entity / relation. /reference/search_modes.md"
  - id: original-brief
    text: "Original Requirements brief — the verbatim user prompt and traceability matrix. /explanation/requirements.md"
  - id: release-log
    text: "Update log — the bundle-root append-only release-record entries (log.md, a reserved filename)."
---

# What changed in okf-loom v2

> **Scope:** this page preserves earlier tranche outcomes and their release
> limitations; historical “pending” review/capture statements are not today's
> maintenance task list or permission gates. The
> [maintenance plan](/explanation/maintenance_plan.md) owns current priorities.
> The proposal availability section below reflects the implemented experimental
> backend; no new human accessibility, screenshot, or release acceptance is claimed.

okf-loom started as a request to turn the upstream OKF format into a
cloneable, harness-agnostic skill: validate, search, discover gaps in,
and render a navigable wiki for any Open Knowledge Format bundle. That
brief is captured verbatim in the
[Original Requirements brief](/explanation/requirements.md), and the
current machinery that delivers it is normative in the
[okf-loom specification](/reference/spec.md).

This page is the honest, one-screen answer to *"I have okf-loom — what
is actually new, and what changed?"* It is a summary, not a re-statement
of the spec. Every claim below links to the concept that proves it.

## How okf-loom grew

The dates are the release-record entries in the bundle-root update log
(`log.md`);

the contracts are in the [specification](/reference/spec.md).

1. **Docs bundle foundation (2026-06-29 → 2026-07-01).** The bundle was
   created as a [Diátaxis](https://diataxis.fr/)-structured documentation
   surface, and the long-form design sources were imported as first-class
   concepts so search, graph, and indexes could reach them. The split
   specifications were consolidated into one
   [canonical spec](/reference/spec.md).
2. **v1.0 baseline + Live Studio (2026-07-03).** The dependency-light
   viewer, validate/search/discover/update verbs, and the
   [live collaborative studio model](/reference/spec.md#10-live-collaborative-studio-model)
   — user directs via comments, agent authors live, no full-page refresh —
   shipped from a checkout with no primary install step. The rationale is in
   [Why the live studio exists](/explanation/live_studio_design.md).
3. **Live Studio maturation (2026-07-04).** The friction-report round added
   block-level partial updates (`update-section`, `replace-text`), mid-thread
   `comment-reply`, user-side comment body editing, runtime tunnel attach,
   and live presence.
4. **Full search + deterministic Context Packs (2026-07-11).** Search became
   a complete six-mode surface — see [Search modes](/reference/search_modes.md)
   — and the deterministic, citation-ready
   [Agent Context Packs](/reference/context_packs.md) shipped across the CLI,
   live HTTP, and portable (static / single-file) viewer builds, with a
   content-addressed passage corpus that is byte-deterministic across builds.
5. **Annotations v2 (2026-07-11).** The anchor model was replaced. The
   normative contract is
   [spec §12.1](/reference/spec.md#121-annotations-v2-contract-normative-implemented);
   the hands-on version is the
   [Annotations v2 Studio walkthrough](/demo/annotations_v2_showcase.md).

## Old vs new

The single biggest change is how comments attach to words. Everything
else (search, context packs, partial edits, graph health) is additive.

| Concern | Before | Now |
| :--- | :--- | :--- |
| Comment anchors | First match wins; edits silently move or erase the highlight. | Candidate-set resolver intersects evidence and reports `resolved` / `stale` / `ambiguous` / `invalid` health. See [spec §12.1](/reference/spec.md#121-annotations-v2-contract-normative-implemented). |
| Moving wording | Highlight jumps to another occurrence or vanishes. | Explicit [Re-anchor](/reference/comment_lifecycle.md) appends a closed-schema-2 row and follows the target to new wording. |
| Search | Three modes (lexical/tag/entity). | Six modes — lexical, tag, semantic (zero-dep, not embeddings), hybrid (RRF), entity, relation. See [Search modes](/reference/search_modes.md). |
| Evidence for an agent | Free-form copy/paste. | Deterministic, citation-ready [Agent Context Packs](/reference/context_packs.md) with source coordinates, across CLI, HTTP, and portable builds. |
| Edits while reviewing | Whole-concept rewrites. | Block-level `update-section` / `replace-text` partial updates, mid-thread `comment-reply`, and undo. |
| Static / offline viewer | Search "unavailable." | Client-side portable search over a content-addressed corpus; same ranking and evidence as live. |

## New feature inventory

These are the current capabilities; each links to its binding reference.

- **Annotations v2 resolver** — candidate-set intersection, anchor health,
  and explicit re-anchor
  ([spec §12.1](/reference/spec.md#121-annotations-v2-contract-normative-implemented)).
- **Six-mode retrieval** — lexical, tag, semantic, hybrid, entity, relation,
  all zero-dependency ([Search modes](/reference/search_modes.md)).
- **Agent Context Packs** — schema-v1 deterministic evidence packs
  ([context packs](/reference/context_packs.md)).
- **Portable (offline) search** — content-addressed passage corpus, valid in
  static and single-file builds.
- **Block-level partial updates + comment-reply** — `update-section`,
  `replace-text`, and mid-thread replies ([spec §10](/reference/spec.md#10-live-collaborative-studio-model)).
- **Graph health reporting** — advisory graph-health report and metadata
  grouping.
- **Scoped discovery & planning** — `--scope` / `--neighbors` and
  actionability-bucketed suggestions.

## See it yourself

Two links cover the whole surface:

- **Hands-on:** the [Annotations v2 Studio walkthrough](/demo/annotations_v2_showcase.md)
  is the fastest way to feel what is new — select text, post a comment,
  watch an agent resolve it live, re-anchor it to new wording, search six
  ways, and pull a Context Pack.
- **Rendering:** the [Rendering & Feature Showcase](/demo/showcase.md)
  exercises every renderer (Mermaid, KaTeX, syntax highlighting, tables)
  and live feature on one page.

The whole graph is one click away at `/__graph` in a running Studio.

```bash
scripts/okf-loom serve docs-bundle --no-open   # http://127.0.0.1:8787/
```

## Current proof status

This section is deliberately precise, because the honest status matters.

- **Executable suites pass.** The resolver, WAL-backed backend, HTTP
  projection, Markdown block-IR, and browser suites live in
  `tests/test_annotations*.py`, `tests/test_markdown_block_ir.py`, and
  `tests/test_annotation_projection.py`. Six-mode retrieval, Context Pack
  determinism, and portable-corpus parity are pinned in
  `tests/test_retrieval_context.py` and `tests/test_render.py`.
- **The final ReviewAll and canonical screenshot regeneration are still pending.**
  As the update log (`log.md`) records, several ReviewAll passes
  reported **FAIL** and were followed by bounded remediation. Existing
  screenshot bytes in `docs/screenshots/annotations-v2/` become release
  proof **only** after they are regenerated against the current tree and
  accepted by the manifest validator. **No release PASS is claimed here.**
- This page exists to close the user-outcome gap — *you could not see what
  changed* — not to declare the release complete.

## Proposal availability and historical deferrals

**Current availability:** experimental Proposal / Diff Review backend, CLI,
and HTTP support exists. The browser review workflow remains incomplete and
unexposed; direct mode is still the default. See the
[extension specification](/reference/proposal_diff_review.md) for activation,
transport contracts, and release limits. This is not a release-readiness claim.

These are intentionally **not** in this release record (see the update
log, `log.md`):

- **Proposal / Diff Review (historical tranche boundary).** This earlier
  release record did not include the workflow. Its
  [decision brief](/reference/proposal_diff_review_brief.md) is now resolved
  historical provenance, not an open prerequisite to drafting a specification.
- **Broad scale and performance work.** No load/perf hardening is claimed.
- **Harness-specific wiring.** Per the original brief, harness recipes live
  in the [Embedding guide](/reference/embedding_guide.md); no harness code
  ships in this repository.

## Where to go next

- New to the whole thing? Start with
  [/tutorials/install.md](/tutorials/install.md).
- Want the why behind the studio's comment model? Read
  [live_studio_design.md](/explanation/live_studio_design.md).
- Want the binding contract for what changed most? Read
  [spec §12.1](/reference/spec.md#121-annotations-v2-contract-normative-implemented).
