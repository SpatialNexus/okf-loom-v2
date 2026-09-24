---
type: Decision Brief
title: Proposal / Diff Review resolved decisions and requirements
description: A resolved, non-normative decision brief recording the product posture, contracts, user journeys, architecture, and proof accepted as provenance for a future Proposal / Diff Review specification.
resource: /reference/proposal_diff_review_brief.md
tags: [proposal-review, diff-review, decision-brief, studio, roadmap, non-normative]
timestamp: "2026-07-14T00:00:00Z"
aliases: [Proposal Diff Review brief, proposal review requirements, review-gate decision brief]
entities:
  - label: Proposal / Diff Review
    kind: roadmap_capability
  - label: Proposal revision
    kind: provisional_contract
  - label: UpdateOp
    kind: implemented_foundation
  - label: PlannedAction
    kind: implemented_foundation
  - label: Direct mode
    kind: product_posture
relations:
  - type: resolved_by
    target: /reference/proposal_diff_review.md
  - type: informed_by
    target: /reference/spec.md
  - type: informed_by
    target: /reference/architecture.md
  - type: informed_by
    target: /reference/http_routes.md
  - type: informed_by
    target: /reference/comment_lifecycle.md
  - type: constrained_by
    target: /explanation/live_studio_design.md
  - type: follows
    target: /explanation/v2_release_notes.md
provenance:
  - source: /reference/spec.md
    note: Canonical current behavior for planning, direct mutation, Studio routes, expected
      revisions, storage, undo, and trust boundaries.
  - source: /reference/architecture.md
    note: Implemented write funnel, activity history, group undo, proposal seam, and
      Studio architecture.
  - source: /reference/http_routes.md
    note: Implemented /__apply, /__diff, and /__undo request and response behavior.
  - source: /explanation/live_studio_design.md
    note: Product rationale requiring direct edits to remain the default and proposal
      review to remain opt-in.
  - source: Gold Standard 2026 retrieval-tranche record, prioritized roadmap §2 (retired
      maintainer record; not in the published tree)
    note: Roadmap requirement for a versioned proposal, per-operation review, conflict-checked
      atomic apply, and undo linkage.
citations:
  - id: current-spec
    text: Current okf-loom specification §§7, 10–14, and 17. /reference/spec.md
  - id: studio-architecture
    text: Architecture — Studio write funnel, session storage, proposal seam, conflict
      handling, and group undo. /reference/architecture.md
  - id: route-contracts
    text: HTTP server routes — implemented /__diff, /__apply, and /__undo contracts.
      /reference/http_routes.md
  - id: studio-posture
    text: Why the live studio exists — transparency plus undo, not a review gate by
      default. /explanation/live_studio_design.md
  - id: roadmap
    text: Gold Standard 2026 roadmap §2 — Proposal/diff review (retired maintainer record;
      not in the published tree).
---

# Proposal / Diff Review requirements and decisions

## Purpose and document status

> **Historical scope:** the constraints and availability descriptions below
> record the 2026-07-14 decision brief, not today's runtime inventory. Use the
> [proposal extension](/reference/proposal_diff_review.md), [current constraint coverage](/reference/config_yaml.md#constraints),
> and [maintenance plan](/explanation/maintenance_plan.md) for current behavior
> and priorities. Historical review requirements do not gate unrelated approved
> maintenance; experimental release requirements remain separate.

This page is a **resolved, non-normative decision brief retained as historical decision provenance**.
Its Q1–Q37 recommendations are resolved by the binding [Proposal / Diff Review normative extension specification](/reference/proposal_diff_review.md).
It does not define current runtime behavior, authorize implementation, or claim that Proposal / Diff Review is implemented or release-ready.

The intended reader is the product owner, maintainer, or specification author who needs the decision provenance for how an opt-in review workflow should behave before implementers receive a binding contract.
The completed answers under [Resolved decisions](#resolved-decisions) are decision provenance for the separate normative `proposal_diff_review` specification.
This brief does not acquire normative force merely because every recommendation was accepted.

> **Decision note — 2026-07-14:** The user approved all Q1–Q37 recommendations in this session with the blanket direction, “Let's go with your recommendations for the open questions and then push forward with implementation.” Every recommendation was accepted exactly as written, with no exceptions, modifications, or deferred choices. Implementation remains subject to a separately authored normative specification and its proof obligations.

> **Product boundary:** direct agent edits remain the default okf-loom workflow.
> Proposal / Diff Review is an explicit opt-in for work that benefits from review-before-write and must never silently become a mandatory approval gate.

## User outcome and product principles

The future capability should let a user ask an agent to prepare a coherent change, inspect its rationale, exact diff, validation impact, and conflicts, decide operation by operation, and apply the accepted result as one auditable, undoable transaction.
It should add confidence for consequential changes without slowing the current small-edit loop.

The future specification should preserve these principles:

1. **Direct mode remains the default.** Starting the Studio or using a mutator must not opt a user into proposal review implicitly.
2. **Opt-in is visible and scoped.** A proposal or workflow explicitly selects review mode; the UI and CLI always show which mode is active.
3. **Review immutable bytes.** Decisions bind to one immutable proposal revision, not to content that can change after review.
4. **Conflicts fail closed.** Unknown operation kinds, escaped paths, stale revisions, broken dependencies, and uncertain transaction outcomes never degrade to best-effort writes.
5. **Typed operations only.** The proposal contract is a closed set of data operations; it never becomes a shell, arbitrary argv, plugin-code, or general code-execution channel.
6. **Accepted intent stays coherent.** Dependencies are explicit, rejected prerequisites block their dependents, and the accepted set is applied atomically by default.
7. **No invisible rebase.** A stale proposal is regenerated as a superseding revision so the user reviews the bytes that may be written.
8. **Validation is evidence, not decoration.** Review shows the exact validator profile, snapshot, findings delta, and whether results are current.
9. **Audit and undo are first-class.** Decisions, application, activity, and group undo share stable identities.
10. **The server remains harness-agnostic and LLM-free.** External agents create semantic proposals; the server validates, stores, previews, applies, records, and broadcasts mechanical state transitions.

## Existing foundations and honest gap analysis

Proposal / Diff Review should extend current mechanisms rather than rename aspirations as implemented behavior.

### Implemented foundations

| Foundation | Current behavior | Reuse boundary |
|---|---|---|
| `PlannedAction` and `Plan` | Planning emits reviewable action envelopes with rationale/evidence and, where available, typed operation payloads. | Useful input vocabulary, but not yet a versioned proposal aggregate or review state machine. |
| `UpdateOp` | A typed operation carries a known `kind`, concept `target`, and kind-specific `args`; update handlers validate known operations. | Extend as a closed versioned operation union rather than accepting arbitrary commands. |
| `POST /__apply` | Applies one whitelisted `UpdateOp` through the Studio write funnel; it never accepts shell/argv strings. | A future accepted-set endpoint needs a batch transaction contract; current one-op apply is not proof of batch atomicity. |
| `GET /__diff` | Returns a token-protected line diff between two retained revisions of one concept. | Reuse diff vocabulary and authorization, but proposal preview needs immutable proposal bytes and multi-concept aggregation. |
| Expected revisions | `expected_rev` detects a stale target and returns `409` instead of silently clobbering current bytes. | Retain per-operation checks and add a proposal-level expected snapshot. |
| `group_id`, activity, and history | Writes are attributed in `events.jsonl`; snapshots and group manifests support grouped history and undo. | Link one apply transaction to one proposal revision and decision set; do not mistake group undo for atomic multi-op commit. |
| Group undo | A grouped pass can restore its member snapshots together, subject to retention and partial/pruned cases. | The future spec must define stronger proposal-linked undo outcomes and retention expectations. |
| Suggestions consumer | `suggestions` SSE frames are consumed by the built-in Studio and surfaced in the changes timeline. | Notification exists; a complete proposal list, review workspace, and decision controls do not. |
| Validation, parsing, containment, atomic writes | The existing write funnel validates before writing, checks revisions, snapshots prior bytes, uses atomic replacement, records activity, and broadcasts updates. | A batch coordinator should reuse these invariants while adding all-or-nothing commit and recovery. |

### Aspirational or partially documented seams

| Seam | What exists | What must not be claimed yet |
|---|---|---|
| `proposals.jsonl` | `Studio.post_suggestion(...)` can append a small proposal row when `propose_only` is explicitly true and emit a `suggestions` event. | It is not the recommended full schema, durable decision log, revision model, list/read API, or accepted-set transaction protocol. |
| `post_suggestion` | Provides an opt-in producer seam and is a no-op when `propose_only` is not true. | It does not intercept every direct writer, enforce review policy, or provide per-operation decisions. |
| `propose_only` | A documented opt-in hint can steer a cooperating agent toward suggestions. | The current server does not turn it into a mandatory approval gate or prevent other mutators from writing directly. |
| `studio.constraints` | Parsed and preserved as metadata for an external harness. | Constraints are **not server-enforced policy today**; forbidden paths, caps, or propose-only intent must not be described as security enforcement. |
| Suggested review panel | Extension comments describe a panel that could accept/dismiss suggestions. | The built-in consumer currently surfaces a timeline event, not an implementation-ready review experience. |

The future design should treat these seams as compatibility inputs, not as a finished contract.
In particular, no future specification should claim policy enforcement through passive `studio.constraints` until enforcement scope, authority, bypass behavior, errors, and proofs are explicitly specified.

## Proposed end-to-end user journeys

These journeys reflect the accepted target direction. They remain non-normative and do not describe implemented behavior.

### Agent creates a proposal

1. The user or harness explicitly chooses Proposal / Diff Review for a task or proposal; direct mode remains unchanged elsewhere.
2. The external agent submits a closed, versioned proposal containing rationale, affected concepts, typed operations, explicit dependencies, a proposal-level expected snapshot, and per-operation expected revisions.
3. The server validates schema, paths, operation kinds, dependency acyclicity, revision references, and size limits before accepting anything.
4. The server derives canonical before/after bytes, diffs, and validation impact from one snapshot, stores immutable proposal revision 1, and emits a proposal-created notification.

### User reviews summary, diff, and validation

1. The Studio lists proposals by state and opens a dedicated review workspace.
2. The user sees proposal rationale, author, age, affected concepts, operation/dependency graph, expected snapshot, and current conflict status.
3. The user switches between side-by-side and inline diffs, with a single-column mobile fallback.
4. The user sees validation before/after findings, profile, revision, and freshness; stale evidence is labelled and cannot be mistaken for current proof.

### User accepts or rejects operations

1. Each operation starts undecided unless a later answer deliberately chooses another rule.
2. The user accepts or rejects one operation with an optional reason.
3. Rejecting or invalidating a prerequisite automatically blocks dependents; the UI explains the dependency path and never silently changes the user's explicit decisions.
4. Every decision uses compare-and-swap authority tied to the immutable proposal revision and latest decision-row token.

### Conflicts occur

1. The server continuously derives conflict status from current concept revisions but does not mutate the proposal.
2. Apply rechecks both the proposal-level expected snapshot and every accepted operation's expected revisions under the transaction locks.
3. A mismatch returns typed conflicts and writes no content.
4. The agent regenerates a new proposal revision that supersedes the old one; prior decisions remain auditable but do not transfer automatically to changed bytes.

### User applies the accepted set

1. Apply is enabled only when every accepted operation is valid, all dependencies are satisfied, no accepted operation is blocked, and the conflict/validation requirements are met.
2. The client sends an idempotency key and the exact proposal revision plus decision-set token.
3. The server topologically orders the accepted operations, stages all resulting concept bytes, validates the final staged set, and commits all affected concepts atomically by default.
4. One apply transaction, one `group_id`, and linked activity rows identify the result.
5. A lost response can be retried safely; the same key and fingerprint returns the established outcome, while key reuse with different content conflicts.

### User audits or undoes the application

1. The proposal shows who decided each operation, who applied it, the resulting revisions, validation result, activity ids, and undo group.
2. Group undo restores the whole applied transaction or fails closed with a typed explanation; partial restoration is not presented as full success.
3. Undo emits linked activity and leaves the proposal and decision history intact.

### User stays in direct mode

1. The default Studio and CLI workflow continues to apply direct mutator operations immediately.
2. No proposal is created unless the user, workflow, or individual submission explicitly requests one.
3. The UI does not nag for approval, reinterpret existing constraints as policy, or route ordinary edits through a hidden queue.
4. Existing validation, expected-revision checks, activity, snapshots, and undo continue to provide the direct-mode safety net.

## Proposed initial scope

The recommended first release is intentionally narrow:

- one local single-user Studio and its trusted external agent/harness;
- explicit opt-in per proposal or named workflow, with direct mode still default;
- immutable, versioned proposal aggregates and append-only decision/apply records;
- a closed subset of existing `UpdateOp` kinds that can produce deterministic staged bytes;
- explicit operation dependencies with cycle rejection and automatic dependent blocking;
- proposal summary, affected concepts, canonical before/after bytes, diff, and validation delta;
- per-operation accept/reject with reasons and compare-and-swap tokens;
- no-rebase conflict handling through regenerate-and-supersede;
- atomic accepted-set apply by default, recoverable idempotency, and proposal-linked group undo;
- dedicated HTTP read/list/create-decision/apply routes and matching CLI commands;
- keyboard-accessible Studio review in side-by-side or inline form, with mobile single-column fallback;
- bounded local storage, checkpoints, retention visibility, SSE notification, polling/resync parity, and audit linkage.

### Explicit non-goals

- Replacing direct mode or making review mandatory by default.
- Multi-user roles, quorum approval, remote identity providers, hosted-service accounts, or organization-wide policy administration.
- Automatic or semantic rebasing of stale proposals.
- General arbitrary shell commands, argv execution, code execution, package installation, or filesystem writes outside the bundle.
- Treating `studio.constraints` as enforced policy without a separately specified enforcement contract.
- CRDT/OT collaborative editing or an in-browser Markdown editor.
- Reviewing arbitrary binary assets in the initial contract.
- Automatically accepting operations because they came from a trusted agent or passed validation.
- Persisting model prompts, hidden reasoning, or chain-of-thought.
- Claiming that current `proposals.jsonl`, suggestion surfacing, or `POST /__apply` already delivers this workflow.

## Accepted architecture and contract direction

Everything in this section is an **accepted recommendation for future specification drafting**, not a current or normative contract.

### Architecture

1. **Proposal service.** Add a Studio-owned service that accepts closed proposal submissions from trusted CLI/HTTP callers, canonicalizes operation order and paths, captures one immutable expected snapshot, and appends an immutable proposal revision.
2. **Preview engine.** Reuse `UpdateOp` handlers in a side-effect-free staging context to derive before/after bytes, aggregate diffs, and run validation over the complete staged bundle snapshot.
3. **Decision store.** Append full, typed decision rows with stable proposal/revision/op identities and row tokens; derive latest decision state rather than mutating reviewed proposal bytes.
4. **Conflict projector.** Derive current conflict/blocking state from canonical bundle revisions, proposal expectations, dependencies, and decisions; do not rewrite stored proposal data merely because the bundle changes.
5. **Batch apply coordinator.** Preflight and lock the complete target set in canonical order, restage from the reviewed revision, validate, write a recoverable transaction intent/WAL, atomically publish the accepted set, and finalize linked history/activity/outbox records.
6. **Audit and notification projection.** Expose canonical list/read views and emit notification-only SSE frames; reconnect, cursor gaps, and polling refetch canonical proposal state.
7. **Studio review workspace.** Render summary, operation tree, diff, validation, decisions, conflicts, apply outcome, and undo linkage from the canonical read model.

### Accepted contract direction

| Contract area | Recommended direction |
|---|---|
| Identity | Stable `proposal_id`; monotonically numbered immutable `proposal_revision`; stable `op_id` within a proposal lineage. |
| Supersession | A new revision names `supersedes_revision` and preserves old bytes and decisions for audit. |
| Snapshot | Proposal-level sorted concept revision map plus a deterministic snapshot digest; each op also carries expected revisions for every concept it reads or writes. |
| Operations | Closed discriminated union with schema version, typed args, explicit read/write sets, and `depends_on` op ids. |
| Decisions | `undecided`, `accepted`, or `rejected` per op; `blocked` is derived, not a substitute for the user's explicit decision. |
| Proposal state | Derived states such as `draft`, `in_review`, `ready`, `conflicted`, `applied`, `superseded`, `withdrawn`, and `expired`, with one normative state machine. |
| Preview | Canonical before/after content, diff format version, validation profile/result/delta, and freshness bound to proposal revision and snapshot digest. |
| Apply | Exact proposal revision + decision-set token + idempotency key; accepted dependency-closed set; atomic by default; typed terminal or recoverable outcomes. |
| Storage | Append-only proposal, decision, and apply logs with checksummed checkpoints, bounded generations, explicit retention, and fail-closed recovery. |
| API | Dedicated proposal list/read/create, decision, apply, withdraw, and optional preview-refresh endpoints; do not overload one-op `/__apply`. |
| CLI | Matching machine-readable commands for create/list/show/decide/apply/withdraw with explicit mode and no hidden prompt in automation. |
| Security | Existing token, Host/Origin, body caps, path containment, no-edit, and active-code boundaries plus strict proposal-specific limits and closed schemas. |
| Undo | One proposal application maps to one transaction/group identity; undo records link back without erasing proposal or decisions. |

## Resolved decisions

Each answer records the user's blanket acceptance of the recommendation exactly as written.

### Product posture

### Q1. Where should Proposal / Diff Review be opt-in?
**Why this matters:** The scope of opt-in determines whether the feature adds confidence selectively or becomes a hidden approval tax.
**Recommendation:** Support explicit opt-in per proposal and per named external workflow, while keeping direct mode as the global default; do not infer opt-in from starting the Studio.
**Your answer:**
> Accepted as written.

### Q2. May any configuration make proposal review mandatory?
**Why this matters:** A mandatory mode can contradict the Studio's current local-admin posture and create bypass ambiguity across direct writers.
**Recommendation:** Defer server-enforced mandatory review from the initial release; if governance mode is later added, specify it as a separate explicit policy capability with complete bypass and failure semantics.
**Your answer:**
> Accepted as written.

### Q3. Which tasks should the product recommend—but never force—into review mode?
**Why this matters:** Users need useful guidance without mode switching becoming policy by implication.
**Recommendation:** Recommend review for multi-concept, destructive, high-cardinality, or difficult-to-undo changes; keep small reversible edits in direct mode unless the user chooses otherwise.
**Your answer:**
> Accepted as written.

### Proposal identity and schema

### Q4. What identifies a proposal and its revisions?
**Why this matters:** Decisions, retries, audit links, and supersession require identities that cannot be confused with mutable display fields.
**Recommendation:** Use a sortable opaque `proposal_id`, an integer `proposal_revision` starting at 1, and stable opaque `op_id` values within the proposal lineage.
**Your answer:**
> Accepted as written.

### Q5. Can reviewed proposal content be edited in place?
**Why this matters:** In-place edits could make an acceptance apply bytes the user never reviewed.
**Recommendation:** Never mutate a stored proposal revision; create an immutable superseding revision and require fresh decisions for every changed or transitively affected operation.
**Your answer:**
> Accepted as written.

### Q6. What minimum proposal envelope is required?
**Why this matters:** A vague envelope cannot support deterministic preview, conflict checks, or portable audit.
**Recommendation:** Require schema version, identity/revision, author, timestamps, summary, rationale, affected concepts, expected snapshot, typed operations, dependencies, preview metadata, and provenance; reject unknown core fields while reserving a namespaced extensions object.
**Your answer:**
> Accepted as written.

### Operation and dependency semantics

### Q7. Which operation kinds belong in the initial closed union?
**Why this matters:** Every admitted kind expands staging, diff, conflict, rollback, security, and proof obligations.
**Recommendation:** Start only with deterministic concept operations already backed by typed handlers—frontmatter, links/relations/entities/tags, exact text replacement, section update, and concept create/delete if separately proven—and exclude arbitrary commands.
**Your answer:**
> Accepted as written.

### Q8. How should operation dependencies be represented?
**Why this matters:** Implicit ordering makes partial acceptance unpredictable and can produce invalid bundles.
**Recommendation:** Require explicit `depends_on: [op_id]`, reject missing references and cycles at proposal creation, and use canonical topological order with `op_id` as the deterministic tie-breaker.
**Your answer:**
> Accepted as written.

### Q9. What happens when a prerequisite is rejected or conflicts?
**Why this matters:** Applying a dependent alone may violate the proposal's rationale or bundle invariants.
**Recommendation:** Derive `blocked` for all transitive dependents, explain every blocking path, and prohibit apply until the dependency-closed accepted set is valid; never auto-reject on the user's behalf.
**Your answer:**
> Accepted as written.

### Review decisions

### Q10. Which per-operation decisions are user-authored?
**Why this matters:** Mixing user decisions with derived system state obscures authority and audit meaning.
**Recommendation:** Store only `accepted` and `rejected` as user decisions, treat absence as `undecided`, and derive `blocked`, `conflicted`, and `invalid` separately.
**Your answer:**
> Accepted as written.

### Q11. Are rejection and acceptance reasons required?
**Why this matters:** Reasons improve audit and regeneration quality but mandatory prose can make review tedious.
**Recommendation:** Make a reason optional for acceptance and required for rejection only when the rejected op has dependents; always support a short bounded reason field.
**Your answer:**
> Accepted as written.

### Q12. Do decisions survive a superseding proposal revision?
**Why this matters:** Carrying decisions saves time but risks approving changed bytes or changed dependency effects.
**Recommendation:** Preserve old decisions for audit but transfer none automatically; the UI may suggest unchanged operations for one-action re-review only after proving identical canonical op, inputs, outputs, and dependency closure.
**Your answer:**
> Accepted as written.

### Conflict and rebase

### Q13. What constitutes a proposal conflict?
**Why this matters:** Concept write revisions alone may miss stale reads, renamed targets, or changed validation context.
**Recommendation:** Conflict when the proposal snapshot digest, any accepted op read/write expected revision, target existence/type, or required capability differs; return a typed conflict per affected op and concept.
**Your answer:**
> Accepted as written.

### Q14. Should the server rebase a stale proposal automatically?
**Why this matters:** Automatic rebase can change output after review and conceal semantic conflicts.
**Recommendation:** Do not auto-rebase; require the agent to regenerate a superseding revision, recompute preview/validation, and obtain decisions against the new immutable bytes.
**Your answer:**
> Accepted as written.

### Apply, atomicity, and idempotency

### Q15. Is accepted-set application atomic?
**Why this matters:** Partial application can leave cross-concept changes inconsistent even when group undo exists.
**Recommendation:** Apply the complete dependency-closed accepted set atomically by default; allow no best-effort mode in the initial release.
**Your answer:**
> Accepted as written.

### Q16. What authority must an apply request present?
**Why this matters:** Applying against stale decisions or a different proposal revision is equivalent to bypassing review.
**Recommendation:** Require exact `proposal_id`, `proposal_revision`, proposal snapshot digest, latest decision-set token, and a high-entropy endpoint-scoped idempotency key.
**Your answer:**
> Accepted as written.

### Q17. How should retries and uncertain outcomes behave?
**Why this matters:** A dropped response must not duplicate writes, while lost replay authority must not authorize a second transaction.
**Recommendation:** Use a crash-consistent apply WAL and durable result record; same-key/same-fingerprint replays the established outcome, changed fingerprints conflict, and expired uncertain authority fails closed with an explicit indeterminate status.
**Your answer:**
> Accepted as written.

### Storage and retention

### Q18. Where should proposal records live?
**Why this matters:** Current Studio session data is intentionally ephemeral, while review and audit may need restart-safe or longer-lived records.
**Recommendation:** Store proposal/decision/apply logs under the configured Studio session root by default, self-ignore them from git, and make a separately named bundle-local durable audit location an explicit later option rather than silently committing records.
**Your answer:**
> Accepted as written.

### Q19. What storage model should proposal history use?
**Why this matters:** Mutable files complicate concurrency and crash recovery; unbounded JSONL can fill disks.
**Recommendation:** Use append-only full records with checksummed latest-state checkpoints, monotonic generation names, bounded rotations, and fail-closed handling of complete malformed or indeterminate history.
**Your answer:**
> Accepted as written.

### Q20. What retention guarantees should users receive?
**Why this matters:** Diff, audit, retry, and undo can each outlive different underlying artifacts.
**Recommendation:** Define separate minimum horizons for proposal metadata, reviewed bytes/diffs, apply replay authority, and undo snapshots; surface expiry dates and never imply that retained audit guarantees retained undo.
**Your answer:**
> Accepted as written.

### API and CLI

### Q21. Should proposal operations use dedicated HTTP routes?
**Why this matters:** Overloading one-op `/__apply` would blur current direct behavior with a stateful review protocol.
**Recommendation:** Add dedicated versioned list/read/create, decision, apply, and withdraw routes; keep current `/__apply` unchanged for direct mode.
**Your answer:**
> Accepted as written.

### Q22. What should the matching CLI expose?
**Why this matters:** External agents and headless users need contract parity without scraping Studio HTML.
**Recommendation:** Provide `proposal create|list|show|decide|apply|withdraw` commands with stable JSON output, explicit proposal revision, explicit mode, and non-interactive failure by default.
**Your answer:**
> Accepted as written.

### Q23. How should list/read pagination and consistency work?
**Why this matters:** Unbounded lists and skewed proposal/decision/apply reads can produce stale controls or missed records.
**Recommendation:** Use bounded cursor pagination over one canonical snapshot and return a read-model token that qualifies decisions and apply; malformed or pruned cursors require a canonical resync.
**Your answer:**
> Accepted as written.

### Studio UX and accessibility

### Q24. What is the primary review layout?
**Why this matters:** Different changes need context-rich comparison without sacrificing narrow-screen usability.
**Recommendation:** Default to side-by-side diff on wide screens, offer an inline mode, and automatically use a single-column inline layout on mobile without horizontal page overflow.
**Your answer:**
> Accepted as written.

### Q25. What keyboard contract is required?
**Why this matters:** Per-operation review is unusable if decisions, diff navigation, conflicts, and apply cannot be completed without a pointer.
**Recommendation:** Require logical tab order, operation next/previous shortcuts, keyboard accept/reject with confirmation semantics, focus restoration, visible focus, skip links, dialog ownership, and no shortcut interception in editable controls.
**Your answer:**
> Accepted as written.

### Q26. How should validation, conflicts, and dependency blocking be announced?
**Why this matters:** Color-only or transient status can cause users to apply an incomplete set unknowingly.
**Recommendation:** Pair icons/color with text, expose programmatic status and counts, use polite live regions for background refresh, move focus only for user-triggered blocking errors, and provide a persistent issue summary linked to affected operations.
**Your answer:**
> Accepted as written.

### Security and governance

### Q27. Which operation and path failures must fail closed?
**Why this matters:** A proposal is untrusted structured input even when a local agent produced it.
**Recommendation:** Reject unknown schema versions/ops/fields, duplicate JSON keys, path escapes, symlink substitution, stale revisions, oversized payloads, invalid dependencies, and undeclared read/write targets before staging or mutation.
**Your answer:**
> Accepted as written.

### Q28. May proposals contain shell, argv, plugin, or arbitrary code operations?
**Why this matters:** General execution would turn a review feature into a privileged command runner and bypass bundle containment.
**Recommendation:** No; keep a closed data-operation union and require any future active-code capability to use the existing separately consented active-code boundary, never proposal apply.
**Your answer:**
> Accepted as written.

### Q29. Should current `studio.constraints` enforce proposal policy?
**Why this matters:** The metadata is currently passive; treating it as enforcement would create undocumented bypasses and false security claims.
**Recommendation:** No in the initial release; keep it advisory/compatibility metadata until a normative policy-enforcement section defines authority, coverage of every writer, errors, audit, recovery, and bypass tests.
**Your answer:**
> Accepted as written.

### Notifications, audit, and undo

### Q30. What should SSE communicate?
**Why this matters:** Treating transport frames as canonical state causes missed decisions and stale apply controls after reconnects.
**Recommendation:** Emit notification-only proposal lifecycle frames with global cursors and stable identities; clients must refetch canonical proposal state after gaps, reconnect, polling takeover, or unknown events.
**Your answer:**
> Accepted as written.

### Q31. What audit links are mandatory?
**Why this matters:** Review is only trustworthy if proposal, decisions, apply, resulting revisions, activity, and undo can be traced together.
**Recommendation:** Record immutable links among proposal revision, op decisions, actor, timestamps, apply transaction/idempotency identity, validation result, resulting concept revisions, activity ids, and undo group.
**Your answer:**
> Accepted as written.

### Q32. What should proposal-linked undo guarantee?
**Why this matters:** Current group undo can report pruned or partial outcomes; a proposal UI must not mislabel those as complete reversal.
**Recommendation:** Attempt all-or-nothing group undo under revision checks, return typed `undone`, `conflicted`, `expired`, or `indeterminate` outcomes, and require a new reviewed proposal for any compensating forward change.
**Your answer:**
> Accepted as written.

### Migration and compatibility

### Q33. How should existing `proposals.jsonl` suggestion rows be handled?
**Why this matters:** Legacy rows are smaller and lack immutable revisions, decisions, expected snapshots, and proof metadata.
**Recommendation:** Read them as `legacy-suggestion` summaries only, never auto-upgrade or apply them, and require explicit regeneration into schema v1 before review decisions.
**Your answer:**
> Accepted as written.

### Q34. How should existing direct clients and `/__apply` behave after launch?
**Why this matters:** A review feature must not break harnesses or silently reroute direct operations.
**Recommendation:** Preserve current direct endpoints and CLI semantics; add new routes/commands and explicit capability discovery, with no behavior change unless a caller selects proposal mode.
**Your answer:**
> Accepted as written.

### Proof and release

### Q35. What minimum contract matrices must the normative spec contain?
**Why this matters:** Prose alone will leave race, recovery, accessibility, and error precedence ambiguous.
**Recommendation:** Require schema, state-transition, dependency, conflict, apply/rollback, idempotency/WAL, retention, HTTP error, async/cache, security, keyboard, and migration matrices with one proof obligation per row.
**Your answer:**
> Accepted as written.

### Q36. What evidence is required before calling the feature implemented?
**Why this matters:** Existing seams and UI notifications could otherwise be mistaken for the complete capability.
**Recommendation:** Require unit, cross-process, fault-injection, HTTP, browser, no-JavaScript honesty, accessibility, static-link/docs, and end-to-end proposal→review→apply→undo proof against the normative matrices.
**Your answer:**
> Accepted as written.

### Q37. What is the release posture for the first implementation?
**Why this matters:** Users need to distinguish an experimental opt-in from a supported review and recovery contract.
**Recommendation:** Ship behind an explicit experimental proposal-mode capability until all P0 matrices pass, migration behavior is documented, final independent review is accepted, and release notes state direct mode remains default.
**Your answer:**
> Accepted as written.

## Decision register

All rows record the user's 2026-07-14 acceptance of the corresponding recommendation exactly as written.
`Accepted` records the decision provenance but gives this non-normative brief no independent normative force.

| Q ID | Topic | Status | User decision | Downstream spec section |
|---|---|---|---|---|
| Q1 | Opt-in scope | Accepted | Per-proposal and named-workflow opt-in; direct mode remains the global default. | Product posture and modes |
| Q2 | Mandatory governance mode | Accepted | Defer mandatory review; any later governance mode needs a separate complete policy contract. | Product posture and modes |
| Q3 | Recommended review use | Accepted | Recommend review for broad, destructive, high-cardinality, or hard-to-undo changes. | Product posture and UX |
| Q4 | Proposal identity | Accepted | Use opaque proposal/op identities and integer immutable revisions. | Identity and schema |
| Q5 | Immutable revisions | Accepted | Never edit reviewed content in place; supersede it and require fresh affected decisions. | Identity and supersession |
| Q6 | Required envelope | Accepted | Require the recommended closed, versioned proposal envelope and namespaced extensions. | Proposal schema |
| Q7 | Initial operation union | Accepted | Admit only proven deterministic typed concept operations; exclude arbitrary commands. | Operation schemas |
| Q8 | Dependency representation | Accepted | Use explicit acyclic dependencies and deterministic topological ordering. | Dependency graph |
| Q9 | Dependent blocking | Accepted | Derive transitive blocking and require a valid dependency-closed accepted set. | Dependency state machine |
| Q10 | Decision values | Accepted | Store accepted/rejected decisions; absence is undecided; derive blocked, conflicted, and invalid. | Review decision schema |
| Q11 | Decision reasons | Accepted | Bound reasons; require them only for rejection of an op with dependents. | Review decision schema |
| Q12 | Decisions after supersession | Accepted | Preserve old decisions for audit but never transfer them automatically. | Supersession state machine |
| Q13 | Conflict definition | Accepted | Detect snapshot, revision, target, and capability mismatches with typed detail. | Conflict model and errors |
| Q14 | Rebase policy | Accepted | Never auto-rebase; regenerate, re-preview, and re-review a superseding revision. | Conflict and regeneration |
| Q15 | Apply atomicity | Accepted | Apply the complete dependency-closed accepted set atomically; no initial best-effort mode. | Apply transaction |
| Q16 | Apply authority | Accepted | Require exact proposal/revision/snapshot/decision authority plus an idempotency key. | Apply request schema |
| Q17 | Retry and uncertain outcomes | Accepted | Use durable WAL/results, fingerprinted replay, conflict, and fail-closed indeterminate outcomes. | Idempotency and WAL |
| Q18 | Storage location | Accepted | Default to self-ignored Studio session storage; defer explicit durable bundle-local audit. | Storage architecture |
| Q19 | Append/checkpoint model | Accepted | Use append-only records, checksummed checkpoints, bounded generations, and fail-closed recovery. | Storage and recovery |
| Q20 | Retention guarantees | Accepted | Define and display separate retention horizons for audit, reviewed bytes, replay, and undo. | Retention and expiry |
| Q21 | Dedicated HTTP routes | Accepted | Add dedicated versioned proposal routes; preserve direct-mode `/__apply`. | HTTP API |
| Q22 | CLI surface | Accepted | Provide explicit non-interactive proposal subcommands with stable JSON. | CLI contract |
| Q23 | Pagination and consistency | Accepted | Use bounded snapshot cursors and read-model tokens; resync malformed or pruned cursors. | Read API and resync |
| Q24 | Diff layout | Accepted | Default wide screens to side-by-side, offer inline, and use mobile single-column. | Studio UX |
| Q25 | Keyboard contract | Accepted | Require complete keyboard navigation, decisions, focus handling, and editable-control safety. | Accessibility matrix |
| Q26 | Status announcements | Accepted | Provide textual/programmatic statuses, polite updates, focused errors, and linked issue summaries. | Accessibility and async UX |
| Q27 | Fail-closed inputs | Accepted | Reject all listed unknown, unsafe, stale, oversized, or undeclared inputs before mutation. | Security and validation |
| Q28 | No arbitrary execution | Accepted | Exclude shell, argv, plugins, and arbitrary code from proposal apply. | Operation security |
| Q29 | Constraints enforcement | Accepted | Keep `studio.constraints` advisory until a complete normative enforcement contract exists. | Governance boundary |
| Q30 | SSE role | Accepted | Treat SSE as cursor-based notification only and refetch canonical state after uncertainty. | Events, polling, and cache |
| Q31 | Audit links | Accepted | Link proposal, decisions, actors, apply, validation, revisions, activity, and undo immutably. | Audit record schema |
| Q32 | Undo outcomes | Accepted | Attempt revision-checked all-or-nothing undo with typed outcomes; review compensating changes. | Undo state machine |
| Q33 | Legacy suggestions | Accepted | Expose legacy rows as summaries only and require explicit schema-v1 regeneration. | Migration |
| Q34 | Direct-client compatibility | Accepted | Preserve existing direct behavior unless the caller explicitly selects proposal mode. | Compatibility |
| Q35 | Required matrices | Accepted | Include every recommended contract matrix with proof obligations per row. | Verification plan |
| Q36 | Implementation evidence | Accepted | Require the complete recommended proof suite against normative matrices. | Proof matrix |
| Q37 | Release posture | Accepted | Keep proposal mode experimental until all stated proof, migration, review, and release gates pass. | Release criteria |

## Spec drafting plan

This historical drafting plan is now fulfilled by the [normative specification](/reference/proposal_diff_review.md); the requirements below remain provenance for reviewing its completeness.

1. **Carry forward the decision register.** Treat all Q1–Q37 recommendations as accepted exactly as written and check the resulting contract for internal contradictions during drafting.
2. **Freeze terminology and identities.** Define proposal, proposal revision, operation, expected snapshot, decision set, blocked state, conflict, apply transaction, audit record, and undo transaction once.
3. **Write closed schemas.** Specify proposal/revision, typed operation unions, dependency edges, decision rows, validation evidence, conflict rows, apply requests/results, WAL/result records, SSE notifications, checkpoints, and legacy projections with duplicate-key and unknown-field rules.
4. **Write state machines.** Cover proposal lifecycle, per-operation decision/derived state, supersession, apply and crash recovery, retention/expiry, and proposal-linked undo; state ownership and compare-and-swap tokens must be explicit.
5. **Define deterministic staging.** Specify snapshot capture, read/write sets, operation ordering, before/after derivation, diff format/version, validation profile and delta, final staged validation, and freshness invalidation.
6. **Define APIs and errors.** Document every HTTP route and CLI command, request/response shape, pagination, authentication, body caps, idempotency, status codes, error precedence, no-edit behavior, and capability discovery.
7. **Define async and cache behavior.** Specify SSE as notification-only, global cursor ordering/deduplication, polling parity, canonical refetch, gap/resync behavior, request ownership, stale-response fencing, BFCache/navigation handling, and apply-button freshness.
8. **Define security and governance boundaries.** Include closed operations, containment, symlink/race defenses, lock order, resource caps, active-code separation, direct-mode compatibility, and an explicit statement that passive constraints are not policy.
9. **Define Studio and keyboard behavior.** Specify desktop/mobile layouts, diff semantics, focus order/restoration, shortcuts, dialogs, live announcements, forced colors, reduced motion, no-JavaScript honesty, and conflict/blocked explanations.
10. **Build proof matrices.** Map every schema rule, state transition, error, race, fault point, keyboard action, security boundary, migration case, and release claim to executable evidence and an owning test layer.
11. **Publish separately.** Publish the normative `/reference/proposal_diff_review.md`; retain this brief as non-normative provenance rather than rewriting history. **Completed 2026-07-14.**

## Acceptance bar used for the implementation-ready specification

The normative specification was drafted against all of the following requirements:

- Every decision-register row is resolved or explicitly deferred outside initial scope, with no blank authority-bearing choice.
- Product posture states unambiguously that direct mode remains default and proposal review requires explicit opt-in.
- All persisted and wire schemas are closed, versioned, bounded, and illustrated with valid and invalid examples.
- Proposal, decision, conflict, supersession, apply, recovery, expiry, and undo state machines are complete and mutually consistent.
- The operation union and dependency semantics define deterministic read/write sets, ordering, blocking, and final staged bytes.
- Snapshot, per-operation expected-revision, no-rebase, validation freshness, and diff-binding rules prove that reviewed bytes equal applied bytes.
- Atomicity names the exact transaction boundary and defines lock order, staging, WAL, commit, rollback/compensation, outbox, replay, and indeterminate outcomes.
- Every API and CLI operation has authentication, authorization boundary, body/size limits, idempotency, response schema, error code, and precedence.
- Storage defines append/checkpoint integrity, rotation, retention horizons, pruning, corruption handling, migration, and the distinction between audit retention and undo availability.
- Studio behavior is specified for keyboard-only, mobile, forced-colors, reduced-motion, screen-reader announcements, no-JavaScript output, reconnects, and stale async responses.
- Security explicitly excludes shell/argv/arbitrary code, fails closed on unknown operations/paths/revisions, and does not misrepresent `studio.constraints` as enforcement.
- Compatibility proves existing direct CLI/HTTP behavior is unchanged unless proposal mode is explicitly selected.
- Proof matrices cover happy paths, every state/error row, concurrency, cross-process locking, crash injection, lost responses, retention boundaries, malicious input, accessibility, and end-to-end apply/undo.
- Release language cannot say “implemented,” “supported,” or “ready” until those matrices pass and independent review accepts the evidence.

## References

- [Current okf-loom specification](/reference/spec.md) — implemented behavior and binding current contract.
- [Architecture](/reference/architecture.md) — write funnel, Studio storage, conflict handling, suggestion seam, activity, and undo.
- [HTTP server routes](/reference/http_routes.md) — current `/__diff`, `/__apply`, and `/__undo` behavior.
- [Comment lifecycle and archive rules](/reference/comment_lifecycle.md) — append-only row, compare-and-swap, WAL, notification, and retention patterns already proven elsewhere in the Studio.
- [Why the live studio exists](/explanation/live_studio_design.md) — direct-authoring product posture and the boundary against a default review gate.
- [What changed in okf-loom v2](/explanation/v2_release_notes.md) — current release status and the honest deferral of Proposal / Diff Review.
- Gold Standard 2026 retrieval-tranche record, “Prioritized roadmap” §2 (a retired maintainer record, no longer in the published tree) — source roadmap outcome for versioned proposals, per-operation review, atomic apply, activity, and undo.

Return to [Reference](/reference/index.md).
