---
type: Plan
resource: /explanation/maintenance_plan.md
title: Fork-first maintenance and delivery simplification
description: Current maintenance priorities and the detailed security, documentation, and workflow impediment inventory for the v2 fork.
tags: [maintenance, architecture, security, documentation, delivery]
timestamp: "2026-09-08T18:15:40Z"
---

# Purpose and authority

This is the single current cross-project maintenance plan. The user approved
fork-first delivery, persistence of this plan, the correctness-first direction,
and an audit of unnecessary security and documentation restrictions. It is a
plan, not a new runtime specification or a release-readiness certification.
The [product specification](/reference/spec.md) remains the behavioral contract;
the [proposal extension](/reference/proposal_diff_review.md) governs its explicit
experimental scope. Changes to those contracts must accompany implementation.

Keep useful safeguards; remove needless obligations. Authentication, capability
checks, denied-action auditing, isolation, unknown-field preservation, data
integrity, concurrency, idempotency, and recovery are not paperwork. Conversely,
Git topology, old screenshot status, copied test totals, and reviewer names do
not by themselves prove product correctness.

## Current state

**Viewer usability follow-up — complete on `fix/viewer-layout-usability`:**
The reading canvas and code blocks now share aligned, responsive bounds;
filter rows wrap coherently and Back to Studio has a visible button affordance.
Single-file exports explain their JavaScript requirement, and print output
uses a readable palette without application chrome. Inline-code link labels
retain their text, and browser document 404s offer Home/Search recovery while
non-HTML errors and public capability boundaries stay unchanged. Focused
regressions, a live browser walkthrough, and independent closeout passed.
The live preview includes these changes. The previously delivered archive and
`ui-patch` described below remain the earlier baseline; this follow-up has not
been merged, published, or repackaged.

**Live documentation review — engineering complete, final review passed,
local delivery ready (2026-09-19):** The user approved the full feedback/navigation/layout plan,
opt-in comment-only sharing, and optional portable-export links back to live
Studio. After the user-requested pause, work resumed autonomously from the
qualification checkpoint. The preserved focused repairs were reviewed intact and
committed: portable `Graph.navigation` diagnostics serialized as target-null
records without fake nodes or unresolved counts, the lifecycle actor taken only
from explicit claim/resolve evidence, the faithful DOM `getElementById` mock,
and the `innerWidth` zoom-identity capture contracts with their narrow oracles.
The three remaining process-ownership repairs are completed with focused proof
on Python 3.11 and 3.12: the cross-process cursor burst proves 501 events
drained exactly across real 16 KiB rotations with no retention resync and
unconditional owned-child terminate/join/kill/reap/close plus a deterministic
failure-path test; descriptor ownership is asserted per `(fd, device, inode)`
identity with unrelated-finalization and sentinel coverage instead of a
process-wide count; and headed-browser cleanup owns its process group from
launch, stops TERM-resistant descendants with bounded TERM→KILL, proves
quiescence before profile deletion, and retains the profile with a visible
error otherwise on an explicitly owned lifetime: the profile is a plain
`mkdtemp` path whose only removal is the proven-quiescent deletion, with no
automatic finalizer. Real-process handshake regressions pin the group
behavior plus retention that survives owner release and exit of the helper
interpreter, and pin the launcher allocation itself as lifetime-free. The earlier
qualification commits (selection lifecycle, table projection/escaped-pipe
parity, adjacent tables, internal fragments, truthful agent attribution, CRLF
list projection, native self-fragments, dismissed-selection retirement, quiet
navigation cancellation, and the independently reviewed validator identity)
stand. Security and identity reviews passed their bounded scopes. Combined core
qualification now passes on Python 3.11 and 3.12. Every expected integration test
executed against disposable real upstream fixtures recovered at their historical
pinned revision, rather than being skipped. The full required-browser gate passes,
including genuine headed Chrome keyboard zoom at 200% and 400%. Its first run
exposed a test-only boot-settle race: an undefined revision passed a null-only
check. Requiring the committed revision repaired that setup without changing
production behavior; the focused cancellation cases and subsequent full browser
gate pass. Strict docs and all shipped samples, JavaScript syntax and reviewed
runtime-identity checks pass.

**Independent final functional review returned two findings; both repaired and
closed by the targeted re-review (2026-09-19):** the review accepted the existing
core, integration and browser qualification evidence and found no material
production regression, but returned verdict FAIL on two points, repaired here
with focused Python 3.11/3.12 proof. First, the retained-profile guarantee was
not durable: the profile was allocated as a `TemporaryDirectory`, so its
automatic finalizer could delete a profile just reported as retained for
diagnosis once the owner reference was released or the helper interpreter
exited. The profile now has the explicitly owned `mkdtemp` lifetime described
above, and the handshake suite gained regressions proving actual retention
after owner release and after helper-interpreter exit, plus the allocation
lifetime pin. Second, `comment-reply --actor` help invited named identities
that the durable create validator then rejected opaquely; the CLI now accepts
exactly the documented closed create role at the boundary (see the resolved
operator-route observation below) without any receipt-schema or authorization
change. Runtime identities are unchanged by these edits (cli/tests are outside
the reviewed manifests) and were re-verified rather than refreshed.

The root coordinator also completed an actual external-agent listening exercise:
a browser request reached the supported listener, the agent claimed and clarified
in-thread, the browser's answer woke it, and an attributed mutator plus resolution
survived browser reload without self-enqueue or duplicate directives. The documented
default reply actor was used; arbitrary named reply actors are not established by
that proof. The listener and owned local servers/display processes are stopped.
The targeted re-review of the two final-review repairs returned no findings and
accepted the retained qualification evidence. The committed-source archive
smoke passed: the maintained deterministic builder verified the commit-derived
inventory, content, modes, paths and exclusions, and the extracted checkout
passed the CLI version/help, strict documentation, all shipped samples, render,
and configured and unconfigured Review-in-Studio export checks. Local delivery
is ready and completes from this final commit: the archive is rebuilt from
exactly this revision, atomically replaces the ignored working-copy
`okf-loom-v2.zip`, and `ui-patch` fast-forwards here — local steps only, with
Git history carrying the outcome. The program's session handover notes were
internal working notes and are not part of the published tree; completed work
is not restarted from them.
Engineering qualification is not final delivery or public release approval. No
push, publication, deployment or public endpoint is part of this local program.

**Sequence step 1 (existing concept collaboration) is repaired on
`feat/studio-feedback`.** Page comments and browser replies send the server's
exact `{"kind": "concept"}` anchor with the target in top-level `concept`;
canonical page, reply and legacy `ref` rows load as page-level rows instead of
failing closed; Source selections become exact `text-quote` anchors only when
the pane is a verified view of the canonical source and the span sits in one
block, and otherwise fall back to a page comment that keeps the selection as
advisory `detail.selection` context; graph-preview selections open the same
fallback composer; lost-response retries reuse the Idempotency-Key and create
one directive; rejected posts keep the draft. The durable proof is
`tests/test_studio_collaboration_real_browser.py`, which drives a real browser
against the production HTTP server and the CLI `wait` → `comment-claim` →
`comment-reply` → mutator → `comment-resolve` loop. Combined delivery completes with the local delivery recorded in the
current state above.

**Durable backend slice and its frontend integration are implemented.** New
document targets (`document:"path.md"`, page anchor) cover actual concepts,
indexes and logs without fake concepts or graph nodes. The page read route,
log reading adapter and bootstrap identity provide universal UI seams. Existing
concept rows, exact anchors, queue/agent replies, WAL replay, row locks and
unknown data remain compatible. Reserved page rows require the new runtime;
do not downgrade their session state to an older reader.

`--allow-tunnel-comment` grants only user create/reply using a separate public
token and existing generation/drain fences. Public read-only and explicit edit
modes remain distinct; forbidden operations are privately audited. Permission
proof uses real dedicated loopback ingress, not a tunnel. `viewer.review_base_url`
adds validated credential-free export metadata for static/single-file review
links, with read-only explanatory metadata when absent. No endpoint was exposed.

Graph diagnostics now separate reserved navigation from topology and verify
rendered heading fragments. Backend graph bodies normalize relative links from
their source and expose reserved-document bodies separately. Directory authored
content/heading targets and static log pages remain readable. Demo billing links
now explicitly distinguish the actual checkout API from unmodelled subscription
billing, and its authored index lists currencies; the showcase orphan is untouched.
One genuine docs fragment error (`#__context` versus `#context`) is corrected.
Self-fragments on their own live/static concept page now retain native `#heading`
URLs; graph/single-file payloads keep canonical document destinations. Real-browser
proof confirms that local fragment clicks retain the page and unsent draft, while
live/static graph navigation and single-file reserved-document reading still pass.

The CRLF defect identified during step 1 is repaired in the backend: rendering
uses the exact canonical body suffix rather than the parser's LF-normalized
body for annotation owner coordinates. Real HTTP tests prove a complete CRLF
projection and exact emoji quote persistence. The final list repair splits CRLF
newlines out of normalized identity runs without changing canonical spans/block
ids or broadening the client decoder. The projection regression module and real
HTTP/browser LF/CRLF list continuation, Unicode exact capture, durable reload and
existing newline-decoder checks pass. The deep nested-list assertion now checks
the separate text/newline segments against actual rendered text, rather than
requiring the defective mixed-width segment.

Focused page/HTTP/CLI/recovery, public capability, navigation/export/config,
projection and graph tests pass. The validator runtime identity was independently
reviewed, explicitly approved and installed; its drift and stale-authority apply
checks pass. Graph heading semantics and review-base config affect validation,
and its manifest includes the document/navigation and actual Markdown
heading-renderer dependencies. Serializer and operation identities were unchanged.
No constants were blindly refreshed. The UI now consumes `bootstrap.document`/`comment`,
`/__data/page`, document comment filters/page rows, canonical graph links and
`okf-review-bootstrap`; combined engineering proof passes and the final
functional review closed without remaining findings.

**UI integration (sequence steps 3, 4 and 6, and the UI half of 5) is
implemented, engineering-qualified and final-review-passed on
`feat/studio-feedback`.** Page feedback by document target works on concepts,
root and directory indexes, logs and the concept selected in the graph, with a
page-scoped list, saved-only counts and truthful, evidence-based agent
availability. Comment-only sessions comment and reply and hold no edit power.
Selections end in an exact anchor or an explained page comment; a page whose
source map cannot be verified client-side still loads and takes page comments.
Reserved log pages read in the application shell with real link destinations,
and graph detail links resolve in live, static and single-file output. The
previously confirmed layout defects are repaired: the workspace fills the
article while prose stays on one bounded lane, Related, Comments and Split are
admitted by measured remaining width, Source has a wrap control, overflow is
local and keyboard reachable, and the single-file concept list scrolls and opens
every entry. Static and single-file exports present Review in Studio from
`#okf-review-bootstrap`, or say why it is unavailable. The rules are recorded in
[the specification](/reference/spec.md). Durable proof is
`tests/test_studio_feedback_real_browser.py`,
`tests/test_studio_layout_real_browser.py` and
`tests/test_studio_navigation_exports.py`, all real browser against the
production HTTP server or real exports.

Qualification outcome and remaining limits:

- **Genuine browser-owned 200% and 400% zoom passes in the final full browser
  gate**, including short/rich documents, retained drafts/focus, Escape, Comments,
  Source/wrap and local overflow. Keyboard-driven Chrome zoom, not CSS zoom or
  viewport resizing, supplies this proof. Earlier red runs remain historical
  failures, not passes or current blockers.
- **Repaired nested-list child ownership (R5):** the client
  `canonicalBlockFor` now resolves an unkeyed nested `li` to its existing verified
  enclosing top-level list item, and LF and CRLF browser probes pin child-only
  selections — including inline markup, repeated child text, and deeper nesting —
  to exact canonical spans with visible marks through public persistence.
  Selections crossing owners or structural marker gaps (outer text into a child,
  across nested siblings, across top-level items) keep the honest explained page
  fallback. No new keys, decoder broadening, or authority changes were involved;
  the proof lives in `tests/test_studio_feedback_real_browser.py`.
- The process-ownership test defects exposed by the full Python 3.11/3.12 runs
  are repaired and committed: the 501-event cursor burst across 16 KiB
  rotations with deterministic owned-child cleanup, per-descriptor FD ownership
  with unrelated-finalization coverage, and owned-group headed-browser cleanup
  with real-process handshake regressions.
- Real integration fixtures were recovered from
  `GoogleCloudPlatform/open-knowledge-format` at historical commit
  `ad30107c31c06aec8a7d5636e0d1058118604e6f`, extracting only the `ga4`,
  `stackoverflow` and `crypto_bitcoin` bundles into disposable storage. Every
  expected test executed and passed; the owned binding and fixture copy were
  removed after identity-checked teardown. The original inputs and v1 reference
  were not used as writable fixtures.
- The security and identity reviews are complete for their reviewed source.
  Combined engineering qualification and the actual external-agent listening
  exercise are complete. The independent final functional review ran
  (2026-09-19), its two findings are repaired as recorded above, and the
  targeted re-review returned no findings. The committed-source archive smoke
  passed, and the local delivery — final-commit archive rebuild, atomic
  replacement of the ignored working-copy ZIP and the `ui-patch` fast-forward —
  completes this program locally. The local display server is stopped; no
  public endpoint exists and none is authorized.
- **Operator-route observation resolved without schema change (2026-09-19):**
  `comment-reply --actor` was rejected before directive creation under the
  documented default, but the help invited named identities that then failed
  opaquely. The CLI now accepts exactly the documented closed create role
  `user|agent` at the boundary and rejects other values — including names such
  as `aic/Polaris` — with an actionable usage error before any directive row is
  created; its help and the CLI reference state that named identity belongs to
  claim/resolve attribution. The persisted receipt schema, authorization
  policy and named claim/resolve attribution are unchanged, and a regression
  pins that a rejected actor creates no row. The successful default-route loop
  does not certify arbitrary reply labels; none are claimed or supported.

### Approved delivery sequence

1. Repair the comment frontend/backend contract, canonical row loading, replies,
   retry reconciliation and Source handling. Preserve exact-anchor validation;
   use an explicit page/block fallback when exact mapping is unavailable.
2. Correct graph diagnostic classification and source-aware navigation. Audit
   genuine authored errors individually; retain intentional orphan examples,
   reserved-document semantics and hand-authored indexes.
3. Make document/Source workspaces use available width while keeping readable
   prose bounded. Allocate Related, Comments and Split by remaining usable width;
   restore single-file scrolling and preserve accessible local overflow.
4. Add consistent feedback across live articles, titles/metadata, supported
   blocks, root/directory/log documents and generated page regions. Use stable
   document/page targets without fabricating concept identities or text offsets.
5. Add explicit opt-in comment-only sharing, separate from content editing,
   apply/undo and agent administration. Public access remains read-only by
   default. Preserve ingress, token/Origin/Host, auditing and resource controls.
6. Offer optional explicitly configured Review in Studio links in static and
   single-file exports. Exports remain read-only and must not contain session
   credentials or private comments. Keep the external AI-agent loop; make saved,
   connected/available, claimed and resolved states truthful. No embedded AI
   service is approved by this plan.
7. Verify real browser-to-HTTP-to-directive-to-`wait` behavior, permissions,
   navigation, responsive layout and browser zoom; complete proportionate
   independent review, committed-archive smoke and local consolidation.

The user has already selected the full plan, comment-only sharing and live-review
links. Do not re-ask those settled questions. Local provisioning, logical commits
and safe consolidation may proceed autonomously within this scope; no push,
external deployment/publication, public exposure or relicensing is authorized.
Keep this plan as current-status authority. The separate restart handover is a
historical bridge, not another status tracker. Human accessibility certification,
cross-browser certification, historical-media rights/CDN clearance and actual
publication/reporting destinations remain separate, honestly stated limits.

### Previous Studio closeout (2026-09-17; superseded for live-review readiness)

The selected reading hierarchy, native search
adapter, exact context previews, graph geometry, and progressive Appearance
controls are implemented and locally verified. `ui-patch` remains the supported
consolidation target. Public read-only/local comparisons informed this work
without widening authorization. The search-timeout recovery form avoids the
busy bundle's derived lock while abandoned workers retain their admission slots;
the native-form adapter rejects API-only fields. Final review also identified
stale single-file Context Pack evidence after Clear filters. Reset now invalidates
the preview, prepared copy, manual fallback, and late clipboard UI together;
desktop/mobile keyboard and late-settlement regressions pass.

Fresh complete Python 3.11/3.12 core checks, upstream-bundle integration using the
same restored pinned fixtures, and focused live/portable readiness and single-file
copy-lifecycle browser checks pass. Independent review found no remaining blocker
in the reset repair. Context lifecycle tests open their owning disclosures rather
than relying on obsolete default visibility. The earlier complete browser run
passed after geometry/focus repairs; that is recovered historical evidence, not
a fresh full-browser run or proof supplied by the earlier core runs. Those core
runs preceded the last UI repairs; the fresh core checks close that gap.

Genuine headed Chrome 400% keyboard zoom was executed in the prior closeout:
the recovered worker result records DPR 1→4, 1440→360 CSS-pixel width,
visualViewport scale 1, no CSS zoom, and passing overflow/focus checks. Its
temporary logs and images did not survive restart. This is historical execution
evidence, not fresh visual certification. Human assistive-technology acceptance,
cross-browser certification, and source-bound release screenshot acceptance are
not claimed. Selected-commit archive smoke and local fast-forward consolidation
are delivery operations, not permission to publish.

The local fork implements **loom 2.0.0 / OKF 0.2**.
The 0.2 backend, conservative migration, and cross-view metadata UI are
implemented and locally verified. Complete Python 3.11/3.12 core checks and
focused real live/static/single-file metadata/readiness browser flows pass;
fresh focused flows supplement, rather than repeat, the historical full browser run.
Independent security and completeness review findings are resolved. This does not
recapture or re-certify historical browser/accessibility evidence.
Current docs and shipped examples now distinguish product/format/API versions,
soft optional metadata, advisory trust, and non-executing computation contracts.
The docs and showcase declarations target 0.2; the demo bundle and test fixtures
retain explicit 0.1 compatibility. Existing authored timestamps and historical
logs are preserved, not mass-attributed to the current editor.

Upstream Version 0.2 was refetched on 2026-09-09; the source/date lives in the
[canonical specification](/reference/spec.md). The migration is opt-in apply,
with read-only preview/check, explicit original-producer attribution only,
partial reports, optional preview revision CAS, per-file atomicity, and root-last
forward recovery using Linux atomic exchange with displaced-file verification.
Interrupted or uncertain publication may retain private recovery evidence that
must be inspected before rerunning; this is not a bundle-wide atomic transaction.
No execution/attestation runtime is included. Current decisions
and remaining work are below; dated implementation notes are not competing
version or release-readiness claims.

The built-in YAML reader now accepts ordinary native-writer metadata lists,
namespaced keys, and Unicode escapes without changing unknown keys. Markdown
Context Packs retain source attribution and computation metadata separately from
exact excerpts; canonical metadata bytes make Python/browser budgeting agree.
Relevant validator, serializer, and operation dependencies were independently
reviewed and recomputed before explicit approval of immutable identities. Existing
stale proposals require fresh preview/review. Local readiness does not authorize
push, publication, public serving, or claim comprehensive human accessibility
certification; no such action was performed.

### Earlier maintenance implementation record (before the 0.2 upgrade)

The following record preserves the earlier maintenance outcomes and limits.
Its then-current version, migration, and browser-status observations are historical;
the current state above and upgrade table below supersede them.

Discovery and the impediment audit are complete at source-inspection depth.
Export-safety, mutation outcome/optional-argument fixes, and the bounded
documentation-coherence cleanup have passed focused checks. Other maintenance
fixes remain pending, and nonempty-output adoption policy remains undecided.
Chunk 6 import preservation is also implemented and verified through temporary
source/destination API and CLI regressions: typed authored bytes and retry
no-ops survive, fallback precedence is honored, and malformed peers receive
content-redacted per-file diagnostics while valid peers import. Header metadata
additions preserve ordinary comments/quotes and exact body bytes; complex header
edits retain the existing round-trip serializer's fallback. Focused import/CLI,
discovery-exclusion and import-lock checks, plus forced built-in YAML import
checks, passed. Overwrite defaults and best-effort CLI exit policy are unchanged;
failures are now separate from intentional skips. No migration or exposure change
was made, and no reviewed identity source or evidence artifact was refreshed.
The detailed findings below retain the discovery snapshot, not a second current
status tracker. They are not runtime reproductions unless explicitly stated.
Existing tests cited there are coverage references, not freshly run claims.

The documentation cleanup corrected active index/CLI/migration/privacy/constraint
guidance and proposal availability, added current ownership routes, and archived
older design/release records in place without changing their historical passes.
Temporary-bundle public CLI examples, existing index tests, and two direct Studio
constraint regressions passed; strict bundle validation and `git diff --check`
passed. At that point, `upgrade --check` and unsupported-target `--apply` were
observed to leave bytes unchanged and return zero; upgrade item 1 below now
replaces that placeholder behavior with honest support diagnostics.
This is not an exhaustive documentation audit. Developer setup/browser collection
claims are addressed by chunk 5 below; older architecture timing/lifecycle
generalizations, runtime docstring overclaims and historical snapshots were
not broadly rewritten. Annotation/retrieval evidence-gate separation is now
implemented; actual human accessibility/release acceptance remains pending.

Ordinary annotation provenance/publication tests now use the existing temporary
two-commit fixture instead of historical repository artifacts or real-source
mutations. Relevance checks execute current Python/JavaScript ranking, citations
and bounded Context Packs, with synthetic report verification using only local
temporary history. Focused core checks and the unchanged proposal runtime-identity
gate passed. The [specification](/reference/spec.md#ordinary-regression-versus-selected-release-evidence)
documents explicit selected-context verification. Both checked-in evidence sets
passed their full historical validators in a temporary clean checkout of the
recorded evidence HEAD; neither validates the dirty maintenance tree as current
release evidence. No PNG/report/provenance refresh was performed. Strict release
promotion, publication recovery, and pending manual/browser-owned zoom remain
unchanged. Broader capture ergonomics and source-text test cleanup remain scoped
follow-ons, not claims of completed accessibility or release acceptance.

Chunk 5's lean developer/PR baseline is implemented: pinned Linux developer
requirements, a separately provisioned browser smoke lane, and additive PR CI
for Python 3.11/3.12 with supported Node. Core collection never imports
Playwright, and mixed modules retain their non-browser contracts. Shared live
fixtures copy shipped bundles into temporary storage, report startup failures
with logs, and reap their subprocesses. Missing optional browser tooling can
skip locally; required-browser mode, broken installations, launch failures and
failed product/fixture preconditions fail. The unused ignored, package-free
`uv.lock` was removed; runtime installation and Pages behavior are unchanged.
By owner decision (2026-09-23) both test workflows and the Pages deploy
workflow are now manual-only (`workflow_dispatch`; no push trigger).

The accumulated non-browser, non-integration maintenance gate now passes on
Python 3.12, including import preservation, archive exclusions, version/upgrade,
setgid admission and the explicitly reviewed identity dependency changes. The
callback-lifecycle test now proves invocation-owned kernel descriptor closure
with and without unrelated pre-existing descriptor cleanup, rather than relying
on a whole-process count; runtime lifecycle behavior and identity constants are
unchanged. This supersedes the earlier 3859-pass baseline referenced below.
Python 3.11 pytest and provisioned browser smoke have not run in CI; both test
workflows are manual-only since 2026-09-23. This is core
maintenance evidence, not browser, accessibility, historical release or
deployment acceptance.

Finding 12's bounded dependency corrections and read-only candidate helper are
implemented: bundled YAML compatibility bytes now affect validator/serializer
digests, and session-authority admission bytes affect the operation digest.
Independent invariant review and explicit primary review approved the exact
manifest additions and all three candidate constants; recomputation matched
before installation. The identity-approval blocker is resolved without bypassing
the drift gate. Further dependency review remains bounded follow-up work, not a
claim of complete closure (see finding 12).
Finding 11's setgid activation fix now accepts exact owner-only `0700` or `02700`
directories without permission repair, following the selected policy review.
Independent security and final-code/identity reviews are complete; explicit
approval installed the reviewed operation identity and resolved that blocker
(see below). This is narrow approval, not permission to auto-refresh authority.
The 3859-pass run predates this slice.
Archive content versus
compressor verification and capture selection remain process-simplification
follow-ons, not grounds to remove integrity or recovery checks. Nonempty-output
adoption, public/local proposal coexistence and durable recovery-policy changes
remain decisions; format migration and optional product expansion are not
prerequisites for completing this maintenance gate.

The bounded archive-exclusion slice for detailed finding 8 is implemented and
has passed the focused archive module. Tracked dependency lockfiles and safe
token/private-named code and documentation now survive release. Temporary Git
fixtures provide an explicit expected inventory and run the checked-in launcher
after extraction, importing required fixture modules independently of the shared
builder/verifier filter. No real workspace or handoff archive was packaged.
The original detailed finding below remains the historical audit snapshot.

### Current archive exclusion policy (finding 8)

`scripts/build_skill_archive.py` still reads regular tracked blobs from one
captured commit, not dirty, ignored or untracked workspace bytes. It no longer
denies `uv.lock`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`,
`pnpm-lock.yaml`, `poetry.lock` or `Pipfile.lock`, nor arbitrary path components
containing secret/credential/token/password/passwd/apikey/api_key/private.
Existing concrete local-state directories, key/credential basenames and globs,
cache/build exclusions, root `.gitignore` and historical release exclusions remain;
`.ssh` components are additionally excluded, and `.token` basenames retain their
exclusion explicitly instead of through substring matching. In particular `.git`, `.aic`, `.okf-loom` session
and proposal authority, `.env` variants and credential/key files stay out.

Ambiguous credential-shaped artifacts **block** publication rather than being
silently omitted or guessed safe. The review signal is a delimited whole word
secret(s), credential(s), token(s), password(s), passwd, apikey, api_key or private
in an extensionless basename or a data filename ending in `.json`, `.yaml`,
`.yml`, `.toml`, `.ini`, `.cfg`, `.conf`, `.env`, `.txt`, `.csv`, `.tsv`, `.db`,
`.sqlite`, `.sqlite3`, `.bak`, `.backup` or `.log`. Exact directory components
`secrets`, `.secrets`, `credentials`, `.credentials`, `passwords` and `.passwords`
also require review. Safe namespaces such as `private/` and filenames such as
`tokenize.py` or `private_api.md` are not blanket-filtered. Diagnostics identify
the captured commit and one-based NUL-separated `git ls-tree -rz --full-tree`
record, not the suspicious path or contents. Inspect that record locally; remove
actual sensitive material from the selected release commit, or deliberately
refine the concrete policy after review. There is no automatic bypass.

The bounded PEM private-key scan still rejects planted keys, including in newly
admitted source/docs/lockfiles, without printing their contents. It is **not** a
general secret detector: arbitrary credentials, private registry URLs, personal
data and unknown sensitive locations still need selected-commit review. Neither
an allowed extension nor a successful build establishes confidentiality.
Atomic publication, backup recovery, member/path/mode/content checks, canonical
ZIP/header checks and whole-byte reproducibility are unchanged and passed their
focused regressions. No archive-header trust exemptions were added. Detailed
finding 9 (content versus compressor reproducibility) and capture ergonomics
remain separate pending work; this slice makes no full-runtime, browser or
release-readiness claim.

### Maintained baseline and scope

- The working baseline is this fork, with `ui-analysis` as its supported local
  checkout, not a wholesale upstream replacement. Preserve
  the redesign, retrieval, annotations, proposal infrastructure, and user work.
- Declared product/format versions are loom 2.0.0 / OKF 0.2. Historical
  “Annotations v2” and redesign names are feature labels, not schema declarations.
- Six-mode retrieval, Context Packs, Annotations v2, the UI overhaul, and
  checkpointed directive recovery already exist. Earlier claim-expiry,
  owner-clearing, string-revision reconnect, index-rejection, and lightbox-focus
  concerns are addressed in inspected source and have dedicated tests.
- Proposal backend/CLI/HTTP support exists experimentally. Studio has an opt-in,
  local, inspection-only Proposals panel; the browser decision and apply
  workflow is incomplete and unexposed. Direct mode remains the default.
- Current upstream `develop` is not an ancestor-equivalent replacement for
  this fork. Its hardening commit is not patch-equivalent as a whole; individual
  mechanisms need comparison before porting. Public SpatialNexus ancestry could
  not be established through the attempted API query. No fetch was performed.
- Strict documentation validation passed before this plan was added. That
  establishes documentation conformance, not runtime or release readiness.

## Delivery policy

Use small fork-first changes, one behavior per logical commit/PR. Keep portable
fixes separate from v2 integration. Do not merge upstream wholesale, force-push,
discard redesign history, or package the handoff archive in a release. Preserve
unrelated dirty work. Commit only owned changes; pushing and opening PRs remain
separate publication actions.

Start with the smallest failing public-behavior test, then fix the owner of the
behavior. Use temporary bundles/repositories for mutation and publication tests.
Run focused checks per slice and a proportionate integration check when slices
come together. Do not recapture screenshots or refresh historical benchmark
reports merely because unrelated source changed.

Existing executable gates must be classified and repaired or deliberately
re-scoped, never silently skipped to claim a passing suite. An explicit release
claim still needs the evidence appropriate to that claim. Pending human
accessibility work must remain honestly pending; it need not block unrelated
approved maintenance.

# Prioritized improvement plan

Sizes are relative: S is a narrow change; M spans connected behavior; L involves
durable migration or recovery. Each row excludes unrelated refactoring.

| Order | Chunk and scope | Dependencies / risk | Narrow acceptance |
|---|---|---|---|
| 1 | **Export safety (M), implemented:** source-overlap and symlink refusal, private output-local staging, invocation-owned recovery, cooperating thread/process publication locks, and recursive media exclusion. | Existing replacement behavior is unchanged; any new adoption policy for arbitrary nonempty outputs remains undecided. The reviewed operation identity was updated without weakening its gate; stale proposal authority requires fresh preview/review. | Focused export/render/identity and proposal checks passed: source and unrelated backups survive, rebuilds do not reconsume output media, publication failures restore or retain prior output, and runtime drift refuses apply without content writes. Strict docs validation passed. |
| 2 | **Mutation outcome and argument correctness (S each), implemented:** CLI classification includes `studio_blocked:*`; HTTP apply validates supplied optional types using the existing schema. | Genuine no-op and partial-success contracts, constraints, and authorization are unchanged; no policy/audit-retention expansion or runtime-identity refresh. | Temporary-bundle CLI success/no-op/blocked/conflict and rollback checks passed; HTTP handler checks reject malformed optional values before writing and preserve valid booleans, omitted defaults, nullable fields, and machine-readable reasons. |
| 3 | **Separate routine tests from historical release evidence (bounded slice implemented):** current retrieval behavior/parity, isolated annotation source/evidence and report fixtures, explicit selected-context verification. | Strict release promotion and proposal runtime identity remain material gates; no artifact refresh, exposure/retention changes, or human/zoom acceptance claim. Capture ergonomics remains separate. | Focused core checks passed without historical objects; temporary unrelated commits/dirty metadata preserve selected historical evidence while false promotion, source drift, artifact/citation/parity tamper and publication failures reject. Both stored evidence sets passed full verification only in their matching historical context. |
| 4 | **Coherent documentation (bounded slice implemented):** corrected commands and index ownership, migration and README/privacy overclaims, actual constraints and proposal availability; added session/proposal ownership routes and historical-scope banners in place. | No runtime/security/evidence-gate changes, artifact moves, identity refresh, or new exposure/support destination policy. Broader setup/evidence follow-ons remain separate. | Corrected sequential authoring, default repair preview, and single-file output examples pass through the public CLI on temporary bundles; focused index/constraint tests, strict docs validation, and diff check pass. This plan remains the sole current-status authority. |
| 5 | **Trustworthy developer/PR baseline (M), implemented:** lazy browser collection with mixed core tests retained, disposable serving fixtures, pinned dev sources, independent PR core and opt-in browser smoke lanes. | Python 3.11 support retained; CI covers 3.11/3.12 and provisions Node. Both local core lanes and actual browser execution are now verified at the scope stated above. No runtime installation requirement or Pages change. | Core collection, startup/deadline/cleanup, sample preservation and YAML checks pass. Upstream integration uses disposable fixtures; controlled orphan setup and bounded relevance assertions no longer demand old upstream defects/ranking. Browser verification and its historical/fresh limits are stated above; no human accessibility or publication acceptance claim. |
| 6 | **Import preservation (S–M), implemented:** honor fallback type, copy typed authored bytes without serialization, patch missing metadata, and report individual failed inputs separately from intentional skips. | Overwrite defaults and best-effort exit policy unchanged; additive API errors and content-redacted CLI diagnostics. No migration or identity-source changes. | Temporary source/destination API and CLI tests pass for quotes/comments/unknown keys, LF/CRLF and exact body preservation, fallback/type precedence, malformed peers, retry no-ops, atomic overwrite failure and destination containment. Focused CLI/import-lock/exclusion and forced built-in YAML checks passed. |
| 7 | **Archive and capture ergonomics (bounded finding 8 slice implemented):** precise archive exclusions and redacted failure for unhandled credential-shaped artifacts; content/reproducibility separation and independent capture lanes remain pending. | Concrete sensitive state/key exclusions and PEM checks retained; no general secret-clearance claim. Finding 9, publication recovery and ZIP/header integrity contracts are unchanged. | Focused temporary-repository archive tests pass: safe token/private code/docs and dependency lockfiles survive, extracted launcher imports required fixture modules, sensitive state stays out, suspicious data paths and planted PEM keys fail without payload leakage, and unsafe/tampered archives still reject. |
| 8 | **Runtime-identity and lifecycle ownership (M–L):** automate reviewed identity generation, complete dependency coverage, reduce duplicated group-finalization knowledge. | Preserve reviewed authority and durable schemas; not a mechanical large-file split. | Output-affecting parser changes invalidate stale proposal authority; existing concurrency/recovery proofs remain valid. |
| Later | **Measured performance and optional product expansion:** benchmark retained-feed scans before optimizing. | Representative workload required. Offline assets, Windows, roles, proposal UI, and computation execution are separate choices. | Measured benefit without changing ordering, citations, retention, authority, or recovery. |

# Upgrade plan

The upstream final Version 0.2 prose, not an intermediate commit or reference-agent
implementation, governs compatibility. It supersedes legacy `timestamp` with
`generated.at` and body citations with `sources`; the documented legacy reading
paths remain supported. Optional metadata does not become mandatory because
Loom can display it. See [migration](/how-to/migrate_spec_version.md) for operations.

| Order | Upgrade scope | Size / compatibility risk | Narrow acceptance |
|---|---|---|---|
| 1 | **Version parsing and 0.2 declarations implemented.** | Product 2.0.0 and format 0.2 are distinct from API/storage schemas and reviewed identities. | Complete major.minor parsing; malformed present declarations remain distinct from absence; legacy best-effort reading retained. |
| 2 | **Central metadata readers implemented and reviewed.** | Semantic compatibility; relevant runtime identities explicitly approved after independent review. | Aware real timestamps; fallback only when generation absent; exact stable source ids, scope descriptors, and per-source window override. |
| 3 | **Trust/lifecycle and read-only presentation implemented; browser upgrade work complete.** | Additive live/search/context metadata and legacy-compatible portable inventory metadata. | Bare verification mapping equals singleton list; stable default and inclusive staleness; trust never authorizes; canonical excerpts/citations remain unchanged. |
| 4 | **Browser dependency update complete.** | Compatible asset/load-order choices, not a latest-version mandate. | Cytoscape 3.34.3, Mermaid 11.17.2, highlight.js 11.12.0; retained compatible cytoscape-dagre 2.5.0 and KaTeX 0.16.47. No further dependency install or CI change in this docs slice. |
| 5 | **Computation metadata implemented; execution remains out of scope.** | No new execution protocol, receipt verdict, ABI, or sandbox. | Read/validate/display only; no resource fetching or execution. |
| 6 | **Conservative migration implemented.** | Explicit apply; Linux exchange required; no bundle-wide filesystem transaction or inferred provenance. | Preview/check do not write; partial may succeed; optional revision CAS; legacy preserved; root last; inspect retained recovery evidence before fresh preview/rerun. |
| 7 | **Docs and shipped example alignment implemented.** | Docs/showcase/empty scaffold target 0.2; demo and fixtures remain compatibility examples. | Strict bundle/sample validation; no fabricated provenance, historical log rewrite, screenshot, or publication work. |

Preserve loom frontmatter citations and exact Context Pack line citations as
distinct contracts. Do not let a concept's `generated` provenance mapping confer
root-index regeneration ownership. Product version, format version, storage/API
schemas, and reviewed runtime identities are separate decisions.

Earlier version-only diagnostic approval preceded the actual 0.2 migration and
does not approve later source-identity drift. Existing proposal snapshots/previews
with stale validator, serializer, or operation authority need fresh preview/review;
do not rewrite persisted authority or bypass the existing gate. The 0.2 integration
review and independent recomputation are complete; future drift still requires
explicit review rather than automatic constant refresh.
Dependency choices above describe installed pins, not a vulnerability finding or
an obligation to adopt incompatible newer majors.

# Detailed impediment inventory

Evidence locations below identify the inspected implementation, not immutable
line-number contracts. Each item states the risk and smallest useful change.
Overlapping documentation/security findings are consolidated rather than counted
twice. **REMOVE** refers to the identified restriction, not surrounding safeguards.

## 1. Ordinary export has weaker ownership protection than screenshot publication

**Classification:** tooling defect; **fix / keep data safeguards**.
`render.py::build_site` and `io_utils.py::atomic_replace_tree` accept destinations
without the traced source-overlap admission and use a fixed `.old` backup that
is deleted if present. Missing output parents can also lead to system-temp
staging. This can threaten unrelated data; it is not unnecessary security.
Use source separation, destination-local staging, invocation-owned recovery, and
failure-injection tests. Decide arbitrary nonempty destination adoption separately.

## 2. Screenshot evidence requires a particular Git topology

**Classification:** process-only/stale gate; **remove topology dependency**.
`capture_provenance.py` final validation requires clean attached HEAD with exactly
one parent equal to the recorded source revision. The ordinary
`test_annotations_capture_manifest.py` invokes that phase. Later unrelated work,
detached CI, or a dirty `.gitignore` invalidates historical evidence.
Bind evidence to its selected source and artifact bytes, not the current HEAD's
parent. Test valid historical evidence after an unrelated commit and rejection
of altered relevant inputs. Explicitly revise the corresponding spec gate.

## 3. Relevance regression depends on whole-runtime hashes and old history

**Classification:** tooling/process overhead; **simplify**.
`benchmark_retrieval_relevance.py` hashes runtime/viewer sources, reconstructs
an old checkout, and validates exact report prose. Its test requires the stored
report to match current sources. Unrelated edits and unavailable old objects can
block ordinary tests. Keep the before/after experiment historical; normal checks
should execute current small ranking/citation/budget and Python/JS parity cases.
Prove core regression works without old Git objects while changed behavior fails.

## 4. Immutable capture redundantly demands clean/full Git state

**Classification:** process-only restriction; **simplify**.
`capture_ui_overhaul.py` requires a clean tree before materializing an immutable
commit and rejects shallow storage. It repeats the complete inventory per image.
Select an explicit revision, sanitize replacement behavior, require its actual
objects, and reference one inventory. Test identical committed inputs from dirty
and shallow checkouts. Alternate-object support needs separate containment review;
do not broadly trust ambient Git configuration.

## 5. Capture cleanup has a shared persistent failure dependency

**Classification:** tooling defect with real recovery obligations; **simplify**.
`capture_ui_overhaul.py` retained-state registry rejects an incomplete tail;
`session_authority.py` preserves directory skeletons. One damaged historical
record can obstruct later jobs. Separate atomic per-job uncertain/live recovery
records from bounded successful-cleanup diagnostics. Test corrupt job A does not
block B; retain uncertain secret-bearing state and never remove replaced paths.
Surface changes to mandatory durable bookkeeping before altering its contract.

## 6. Screenshot replacement requires Linux directory exchange

**Classification:** excessive platform restriction for this use; **simplify**.
`capture_annotations_v2.py` and the screenshot spec require
`renameat2(RENAME_EXCHANGE)` for replacement. Immutable evidence generations with
an atomic current-generation pointer may preserve complete old/new views without
this restriction. Test interrupted publication and concurrent consumption.
Do not infer that proposal-apply exchange requirements can also be removed.

## 7. Visual proof has no usable final-completion path in its own output

**Classification:** tooling/process gap; **simplify, keep accessibility evidence**.
`capture_ui_overhaul.py` always produces interim evidence and requires its human
worksheet to say NOT EXECUTED/PENDING HUMAN, while release matrices require real
human acceptance and browser-owned zoom. Whole-matrix execution lacks scenario
selection. Separate automated, real-zoom, and actual human results; allow partial
collection with explicit coverage. A completed human record should reference
immutable evidence, not rewrite the pending template. Never auto-promote missing
human/zoom work. Which evidence blocks a release claim is a surfaced decision.

## 8. Archive secret-name heuristics silently omit legitimate files

**Classification:** stale control/tooling defect; **remove blanket exclusions**.
`build_skill_archive.py` denies lockfiles and path components containing words
such as token/private/secret. Verification derives expectations from the same
filter, so it can certify an incomplete package. No present missing import was
demonstrated. Keep explicit sensitive-state exclusions and actual secret checks;
report suspicious paths rather than silently dropping safe code. Test safe
token-related modules/lockfiles are included, planted credentials reject without
printing them, and the extracted runtime works. Existing PEM detection is not
comprehensive secret clearance.

## 9. Archive content verification and compressor reproducibility are conflated

**Classification:** process-only coupling; **simplify**.
`build_skill_archive.py` rebuilds a canonical ZIP and compares whole bytes as
well as exact members; expected prefix follows the checkout directory name.
Separate safe member/path/mode/content verification from pinned-environment
byte reproducibility. Test renamed checkout, altered/extra/duplicate/traversing
members, and independent reproducibility drift. Cross-compressor failure remains
a hypothesis. Surface the distinction between the two verification claims.

## 10. Read-only sharing globally disables local proposal editing

**Classification:** apparently overbroad exposure control; **investigate then simplify**.
`server.py`, `proposal_review/http.py`, and `proposal_review/activation.py` reject
coexistence, despite dedicated public-listener route admission. A narrower design
could permit authorized local proposal work while denying it on public ingress.
This is an exposure-contract change: keep current behavior until explicitly
decided. Required proof includes copied local credentials and forged forwarding
headers failing on public routes while legitimate local requests succeed.

## 11. Setgid private-directory activation

**Classification:** tooling defect; **implemented under the selected owner-only
directory policy; reviewed runtime identity blocker resolved**. Public `activate` and
`activate_http` previously rejected new repositories inheriting `02700` from
`02755` bundle parents. Clearing setgid was unsafe: successful mkdir does not
prove that the subsequently observed inode was created by this invocation.

The selected remedy admits effective-UID-owned directories with mode exactly
`0700` or `02700`, consistently in strict SessionAuthority components and proposal
filesystem/publication adapters. Linux directory setgid controls group
inheritance, not group access or process credentials. No directory is chmodded
by activation or these adapters, including pre-existing/substituted state.
Group/other access, sticky/setuid bits, wrong owners, symlinks, and observed
identity changes still refuse. Regular authority records and tokens retain
exact `0600`, no-follow, single-link checks. Admission failures close descriptors,
including failed downstream directory validation.

Fresh activation now passes for both entrypoints under `0022` and `0077` umasks,
with inherited groups, private descendants, and unchanged credentials. Focused
tests also cover reopening `0700`/`02700`, external private authority, safe
replacement before first observation, unsafe `02750` replacement with untouched
sentinels/modes, EEXIST, isolated wrong ownership, admission substitutions,
descriptor cleanup, and setgid transaction preparation/recovery. No real server
startup or full-suite rerun was performed for this slice.

This explicitly broadens admitted directory state, not filesystem authority:
reopenable caller-owned `02700` may be accepted without claiming creation
provenance. Existing ancestor containment remains unchanged. Descriptor pinning
does not exclude shared-parent namespace interference or hostile same-UID/root
writers; no new staging-root, privilege separation, or platform mechanism is
introduced. The normative contract is in the
[current specification](/reference/spec.md#private-directory-admission) and
[Proposal Review storage policy](/reference/proposal_diff_review.md#9-storage-integrity-rotation-and-migration).

Explicit final-code review approved and installed only the operation identity
`proposal-ops@1.0+sha256:225b2beec1eb6aee9032b702bff03babadf8b0c482229bd586bf61fa8683859f`.
All three source-manifest digests were independently recomputed before editing
and matched the approved values; validator/serializer identities and all source
inventories remain unchanged. The runtime-identity gate remains mandatory for
future drift. Existing proposals with older operation authority require fresh
preview/review, not persisted-authority rewrites. No migration is performed;
rollback to older implementations can reject retained `02700` directories.
No evidence artifacts or exposure policy were refreshed. Finding 12 subsequently
addresses the session-authority and bundled YAML dependency gaps, not exhaustive
dependency closure.

Focused combined non-server activation/private-directory/security/transaction/
repository checks and runtime-identity/snapshot/preview/frozen-validator/apply
modules pass after the identity update. Existing parameterized authority-drift
coverage rejects stale operation authority without content or transaction WAL
writes; capture/preview and successful apply checks also pass. The historical
broad run above predates setgid admission and was not rerun for this closeout.

## 12. Runtime identity maintenance is manual and its dependency coverage incomplete

**Classification:** material safeguard plus tooling gap; **bounded repair approved
and installed; identity-approval blocker resolved**. `proposal_runtime_identity.py` now includes
`_yaml_compat.py` in validator and serializer inventories: `parse.py` and
`config.py` consume its loader, and `roundtrip.py` consumes its dumper. The
operation inventory adds `session_authority.py`, whose descriptor/private-mode
admission is used by activation. No other inventory entries changed. Explicit
primary review approved all three exact candidate constants, installed only after
recomputation matched; no further production-source expansion was included.

The [proposal identity contract](/reference/proposal_diff_review.md) documents
`python -B scripts/review_runtime_identities.py`: deterministic review candidates
only, sharing the existing canonical digest algorithm with the drift gate.
Disposable-source regressions cover dependency-byte sensitivity, deterministic
derivation and missing-source refusal; production capture keeps reviewed
constants, and stale-authority apply refusal covers all three identity fields.
The reviewed constants remain immutable production authority. The candidate
command neither approves nor writes authority, and future drift still requires
explicit review. Existing stale proposals require fresh preview/review, never
persisted-authority rewrites. The historical 3859-pass run predates this work.
Focused identity, parser/YAML, operation, snapshot/preview, frozen-validator,
validation and apply modules pass, as do forced built-in YAML operation,
frozen-validator and capture/preview checks. All three current identities match
candidate output; strict docs validation and the diff check pass. No broad suite
or historical evidence refresh was performed.

This is not exhaustive dependency closure. The narrow trace also found omitted
`__init__.py` validation constants, `ignore.py` visibility interpretation, and
`proposal_review/validation.py` normalization plus `canonical.py` digest semantics.
Activation also calls the unlisted `studio._configured_session_root` adapter.
Review those connected owners next rather than hashing all runtime imports.
External PyYAML/Python implementation and YAML backend selection are not encoded
by these source inventories; backend parity/identity policy remains a separate
decision. Comment-only byte changes still cause drift by design. No lifecycle,
schema, migration, or evidence-refresh work is included.

## 13. Constraints documentation denies enforcement that code performs

**Classification:** stale/contradictory; **correct docs, investigate policy coverage**.
`reference/config_yaml.md` calls constraints unenforced; `studio.py` rejects
forbidden paths/group edit limits, and `update.py` emits `studio_blocked`.
The audit did not establish comprehensive structured denial auditing across
routes. Document actual coverage, test it, and retain bounded redacted denial
records where required. Do not claim an existing audit guarantee without proof.
Retiring a user-selected constraint needs a decision; correcting its description
does not. Never log tokens, request bodies, or source quote text as a shortcut.

## 14. Privacy guidance makes unsafe absolute promises

**Classification:** stale overclaims; **remove absolutes**.
`resources/gotchas.md` calls workspace-root serving safe because of pruning and
promises session state stays invisible to Git. Includes can override pruning;
ignore files do not untrack already tracked data and writing them can fail.
Explain actual defaults/limits and recommend deliberately selected bundles.
Test tracked/included state warnings without exposing contents. Do not replace
truthful limits with a claim of automatic confidentiality.

## 15. Gold-standard and final-status records compete with current authority

**Classification:** process-only/stale authority; **archive in place and consolidate**.
`docs/design/gold-standard-2026.md` retains uncommitted-tranche status, repeated
suite totals, fresh-review requirements, a precise informational-finding count,
and a deferred-proposal roadmap. UI closeout/release matrices and verification
README files name other current/final candidates. Readers must reconstruct
chronology before knowing what is actually required. These are valuable dated
records, not a global readiness verdict. Keep rationale and limits; remove their
role as current maintenance permission or exact-count gates. This plan owns
current priorities; current contracts and executable checks own correctness.

## 16. Proposal availability is inconsistently described

**Classification:** stale documentation; **consolidate**.
The proposal reference accurately states experimental implemented backend versus
unexposed browser UI; gold-standard and v2 release-note passages still describe
wholly deferred work or questions preceding a specification. Point current
guidance to the extension and label earlier decisions historical. Check actual
activation/CLI behavior and browser unavailable state; do not advertise release
readiness or broaden exposure from historical approvals.

## 17. Canonical copy-paste commands are incorrect

**Classification:** documentation defect; **fix**.
`reference/spec.md` uses unsupported `repair --dry-run` and supplies JSON to the
boolean `--json-value` flag without `--value`. `how-to/build_static_site.md`
treats single-file build's output directory as a filename. Correct to dry-run
default, `--value '["reference"]' --json-value`, and a build output directory
(or the render command for a filename). Execute each corrected example on a
temporary bundle; assert no dry-run writes and the intended artifact location.

## 18. Agent guidance incorrectly warns index regeneration destroys authored prose

**Classification:** contradictory instruction; **fix across linked guidance**.
`SKILL.md` and `resources/authoring.md` say unmarked prose is replaced, while
`index.py` protects unmarked/non-generated indexes. Explain authored skip,
marker-only replacement, explicitly generated regeneration, and frozen refusal.
Use existing index preservation tests; no runtime behavior change is needed.

## 19. Migration documentation presents future behavior as a usable workflow

**Classification:** misleading current contract; **fix documentation and diagnostics**.
`how-to/migrate_spec_version.md` describes preview/idempotent migrations before
admitting they are not implemented. `cli.py::cmd_upgrade` returns zero without
conversion. Lead with actual limitations; move future design to the upgrade
plan. Test unsupported target handling and unchanged bytes. An honest non-success
exit is intended; preserve unrelated established command outcomes.

## 20. Developer setup, collection, and CI promises disagree

**Classification:** tooling/documentation defect; **simplify**.
README promises missing-browser skips, but a browser module directly imports
Playwright before marker deselection. `uv.lock` contains only a Python >=3.12
floor, while docs promise 3.11+. Pages CI is not a PR test lane. Establish one
reproducible dev path and independent core/browser collection; remove an unused
empty lock or make it meaningful, without adding required runtime installation.
Prove supported Python versions rather than silently raising the minimum.

## 21. Some proof tests check source spelling or their own fixture setup

**Classification:** low-value executable overhead; **replace or remove duplicates**.
`test_annotations_capture_manifest.py` includes constructed-set/hash assertions,
source-string/order checks, and tests that modify actual checkout files. Stronger
publication fault tests also exist. Retain tests that call production validators
and publication seams, move mutations into temporary repositories, and delete
redundant setup-only assertions only after preserving the real behavior oracle.
Harmless refactoring should pass; broken validation/publication should fail.

## 22. Marketing and support claims exceed implemented contracts

**Classification:** overclaim/stale identity; **correct**.
README says no sidecar schema/state and full GitHub-flavored Markdown, whereas
collaboration has versioned sidecars and the renderer documents a subset.
SECURITY names upstream develop as supported; clone/demo URLs still identify
upstream. Say authored content remains Markdown with no required database,
separate collaboration state, and the supported Markdown subset. Preserve
upstream attribution. Select the fork publication URL/support branch before
changing those destinations; do not invent a release identity.

## 23. Architecture maps omit current owners while routing readers to old records

**Classification:** navigability/duplicated authority; **consolidate**.
`resources/architecture-map.md` and the architecture reference omit major
session/proposal owners and elevate gold-standard history. Update the existing
reference with public ownership, lifecycle boundaries, and focused test seams;
keep the resource a short route into it. Do not introduce another exhaustive
file inventory or subsystem status tracker.

## 24. Capacity and strict ancestor permissions need evidence, not blanket removal

**Classification:** material safeguards with possible ergonomics costs; **investigate**.
Exact private-mode requirements on external session ancestors may reject safe
owner-controlled layouts. Retained proposal checkpoints and quotas may need a
clear operator capacity/recovery path. Neither exploitation nor exhaustion was
reproduced. Measure and test before changing permission predicates or retention;
keep substitution defenses, uncertain-outcome records, and recoverability.

# Documentation consolidation policy

1. Keep the current product spec, focused references, progressive resources,
   task-oriented how-tos, and explicit experimental boundaries.
2. Archive old design/audit/closeout records **in place first**, with a short
   historical-scope banner and link to current authority. Preserve original
   decisions, candid limitations, and append-only log history.
3. Trace executable consumers before moving or deleting screenshots/JSON/reports.
   Separate historical verification from current behavior tests, then eliminate
   redundant current-status obligations. Do not manufacture replacement proof
   manifests or a new promotion framework.
4. Do not claim historical PASS as current PASS. Remove manually synchronized
   test totals, fixed informational counts, and review-round requirements from
   ordinary maintenance. Git and test runs carry chronology.
5. Update this plan when priorities or completed outcomes materially change,
   not after every command. Do not duplicate it with a new tracker per slice.

# Decisions and boundaries

Proceed with approved reversible correctness fixes and evidence-backed cleanup.
Surface these before making their consequential changes:

- Whether arbitrary nonempty export directories require explicit adoption.
- Local proposal editing alongside a read-only public tunnel: public proposal
  authority must remain denied; current exposure behavior stays until decided.
- Retirement or changed enforcement of user-selected constraints, retention,
  or uncertain-state bookkeeping.
- Release-specific human/zoom evidence acceptance, distinct from maintenance.
- Dropping Python 3.11, selecting a fork publication destination, computation
  execution, or new public capabilities. Local support is assigned to the
  `ui-analysis` checkout; conservative migration is implemented, not pending approval.

No universal unnecessary-security verdict is warranted. The strongest findings
concern proof-process coupling and inaccurate documentation. Keep session/Origin/
Host capability checks, dedicated public ingress, no-edit, active-code consent,
bounded parsing/requests, CAS, private state, locks, WAL, and real recovery.

# Verification limits

The audits inspected source, selected tests, documentation, Git history, and
relevant callers. Primary discovery ran version/help-related inspection and
strict bundle validation, plus read-only GitHub/npm/PyPI queries. It did not run
a full suite, browser accessibility evaluation, vulnerability scan, or performance
benchmark. Each implementation slice must reproduce its claimed failure through
the cheapest relevant public seam before treating an inspection concern as a
confirmed defect. No audit finding itself authorizes weakening a safety contract.
