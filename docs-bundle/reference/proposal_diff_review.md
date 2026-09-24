---
type: Specification
title: Proposal / Diff Review normative extension specification
description: Normative implementation contract for an experimental, explicitly selected review-before-write workflow with immutable proposal revisions, typed operations, atomic apply, recovery, audit, undo, and accessible Studio review.
resource: /reference/proposal_diff_review.md
tags: [proposal-review, diff-review, normative, experimental, studio, api, accessibility]
timestamp: "2026-07-15T00:00:00Z"
aliases: [Proposal Diff Review specification, proposal review protocol v1]
entities:
  - label: Proposal / Diff Review
    kind: experimental_capability
  - label: Proposal revision
    kind: protocol_aggregate
  - label: Direct mode
    kind: product_posture
relations:
  - type: extends
    target: /reference/spec.md
  - type: resolves
    target: /reference/proposal_diff_review_brief.md
  - type: reuses
    target: /reference/http_routes.md
  - type: constrained_by
    target: /reference/comment_lifecycle.md
provenance:
  - source: /reference/proposal_diff_review_brief.md
    note: The user accepted Q1–Q37 without exception on 2026-07-14; this specification resolves those choices into normative contracts.
  - source: /reference/spec.md
    note: Canonical current behavior remains authoritative outside this opt-in extension.
citations:
  - id: decisions
    text: "Accepted Proposal / Diff Review decisions Q1–Q37. /reference/proposal_diff_review_brief.md"
  - id: current
    text: "okf-loom Current Specification. /reference/spec.md"
---

# Proposal / Diff Review normative extension specification

## 1. Status, authority, and scope

This document is the normative contract for Proposal / Diff Review protocol version `1`.
The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.
It resolves all accepted Q1–Q37 decisions in the [decision brief](/reference/proposal_diff_review_brief.md).
Implementation status (2026-07-16): explicit capability activation, the seven
Bundle 3 create/list/show/decide/withdraw/apply/undo HTTP routes, matching CLI commands,
explicit bounded legacy regeneration, repository recovery, and the production Studio notification outbox bridge are
implemented experimentally with executable transport tests. Proposal apply,
proposal undo, recovery, and their transaction-finalization authority are also
implemented experimentally in the transport-neutral core with executable backend
proofs. The Proposal Review Studio decision UI (decide, withdraw, apply, undo)
and its browser proof remain incomplete and are not exposed. An opt-in, local,
inspection-only Studio Proposals panel is implemented: when capability discovery
reports this extension active for a local editable Studio page, the panel lists
proposal revisions and shows one revision's summary, rationale, conflicts,
operations with their decisions, validation, a side-by-side or unified diff,
and the retention dates the read model reports. The served read model currently
carries no replay, undo, or audit retention horizon, no apply/undo result, and
only unfinished transaction attempts, so the panel says that apply/undo history
is not reported rather than that none exists, and it never presents a retention
date as undo availability. Author and decision-actor names are shown as
unverified labels. It uses only the list and show GET routes with the page's
`X-OKF-Token`, refetches the canonical read model after proposal notifications,
bundle edits, live-connection recovery, and BFCache restore, empties proposal
text from the page on pagehide, never POSTs, never receives
`X-OKF-Proposal-Token`, and renders no decision controls. A local read-only
(`--no-edit`) page carries no session token, so it
explains the CLI path instead of requesting; public-ingress pages and exported
sites get no panel. Browser proof: `tests/test_proposal_inspection_browser.py`.
Sections describing the decision workspace remain design requirements rather
than claims of current behavior. Apply/undo has no persisted production
data, so no data migration or compatibility reader is required.

The [current specification](/reference/spec.md) continues to govern implemented behavior.
Direct mode remains unchanged and is the global default.
Starting Studio, declaring `studio.constraints`, or invoking an existing mutator or `POST /__apply` MUST NOT activate, intercept, queue, or reroute work into proposal mode.
Proposal mode is selected only by an explicit per-proposal `mode:"proposal"` submission or by a named external workflow whose invocation explicitly selects that mode.
The initial release MUST NOT offer server-enforced mandatory review.
The product MAY recommend proposal mode for multi-concept, destructive, high-cardinality, or difficult-to-undo work, but MUST NOT force it.

The initial scope is one local single-user Studio, one trusted external agent or harness, Markdown concept files, immutable proposal revisions, per-operation decisions, deterministic preview, atomic accepted-set apply, audit, and proposal-linked undo.
It excludes multi-user approval, quorum, remote identity, organization policy, CRDT/OT, an in-browser Markdown editor, binary assets, automatic acceptance, automatic rebase, model prompts or hidden reasoning, arbitrary filesystem mutation, and arbitrary execution.

## 2. Capability and compatibility contract

The capability id is `okf.cap.experimental.proposal_diff_review.v1`.
It MUST be reported by `scripts/okf-loom capabilities` as `optional`, `experimental`, and with the boolean field `active`.
Inactive and active examples are respectively:

```json
{"id":"okf.cap.experimental.proposal_diff_review.v1","status":"experimental","active":false,"protocol_version":1,"operation_schema_version":1,"direct_mode_default":true,"inactive_reason":"not_enabled"}
```

```json
{
  "id": "okf.cap.experimental.proposal_diff_review.v1",
  "status": "experimental",
  "active": true,
  "protocol_version": 1,
  "operation_schema_version": 1,
  "direct_mode_default": true,
  "inactive_reason": null
}
```

The server may activate editable capability only when `--enable-proposal-review` or the exact embedding equivalent is explicit, the bind address is loopback, no tunnel is attached, the session root passes §9 security checks, startup recovery succeeds, and all required runtime identities are available.
Enabling it with a public/non-loopback bind, `--public`, or an attached/requested tunnel **without** `--no-edit` MUST fail server startup with `proposal_review_requires_loopback`; it MUST NOT silently deactivate the capability or weaken the exposure request.
Read-only public Studio exposure may keep `active:true` and display proposal read models only when current server exposure rules permit GET and `--no-edit` is active; all proposal mutation then returns `read_only` before loopback/body/idempotency checks.
Attaching a tunnel to an active editable proposal session MUST fail `409 proposal_review_requires_loopback`; a tunnel may attach only after restart with `--no-edit`.
When inactive, every dedicated proposal route returns `404 capability_unavailable` and every `proposal` CLI command exits `4` with that code.
Every transport-neutral public proposal service entry point, including create, supersede, decision, withdraw, list, and show, MUST first verify that the capability is explicitly active for that service instance and that repository startup/recovery completed. An inactive service fails `capability_unavailable` before request parsing, capture, cursor verification, repository reads, preview admission, idempotency preparation, or mutation. A read-only exposure may call list/show only through an explicitly active read service; mutation additionally applies the no-edit and exposure gates. No caller may bypass this fail-closed gate by invoking the library service directly.
Activation exposes the new surface but does not alter existing CLI commands, `/__apply`, `/__diff`, `/__undo`, mutators, comment behavior, or direct write semantics.
Unknown clients continue to operate in direct mode.
No existing plan, suggestion, or one-operation apply envelope is implicitly a protocol-v1 proposal.

## 3. Terminology and canonical primitives

| Term | Normative meaning |
|---|---|
| Proposal lineage | All immutable revisions sharing one `proposal_id`. |
| Proposal revision | One immutable reviewed aggregate identified by `(proposal_id, proposal_revision)`. |
| Operation | One closed, typed data transformation with stable `op_id`, declared reads, writes, and dependencies. |
| Expected snapshot | The complete visible concept inventory, exact content revisions, and validation-context authority captured before staging. |
| Decision | A user-authored `accepted` or `rejected` choice for one operation in one immutable revision. Absence means `undecided`. |
| Derived operation state | Server projection of `valid`, `invalid`, `blocked`, or `conflicted`; it is not a user decision. |
| Decision-set token | CAS token over the latest decision row for every operation in one revision. |
| Preview | Snapshot-bound canonical before/after bytes, diff, validation result, and validation delta. |
| Apply transaction | One idempotent, recoverable attempt to publish the dependency-closed accepted set. |
| Audit | Retained identities and outcomes proving what was reviewed and attempted. Audit retention does not imply preview-byte or undo retention. |
| Undo transaction | Revision-checked all-or-nothing restoration of every concept changed by one successful apply. |

All JSON is UTF-8 RFC 8259 JSON.
Objects MUST reject duplicate keys at every nesting level before schema validation.
Public and persisted core objects are closed: unknown fields are errors, except an explicit `extensions` object.
Extension keys MUST match `^[a-z0-9][a-z0-9.-]{0,62}/[A-Za-z0-9_.-]{1,64}$`; values MUST be JSON values no larger than 16,384 canonical bytes each; core readers preserve them but derive no authority from them.
Booleans are not integers.
Strings are compared byte-for-byte after UTF-8 decoding; no Unicode normalization is performed.
Timestamps are UTC RFC 3339 with exactly millisecond precision, for example `2026-07-14T12:34:56.789Z`.

| Primitive | Exact form |
|---|---|
| `proposal_id` | 26 uppercase Crockford Base32 ULID characters, excluding `I`, `L`, `O`, `U`. |
| `repository_id` | `repo_` followed by 26 uppercase Crockford Base32 ULID characters, excluding `I`, `L`, `O`, `U`; server-generated once and immutable. |
| `op_id` | `op_` followed by 26 uppercase Crockford Base32 characters; stable within a lineage. |
| Record id | Kind prefix (`rec_`, `out_`, `act_`, `cor_`, `svc_`) followed by 26 uppercase Crockford Base32 characters. `svc_` identifies only a service mutation WAL intent; `act_` identifies a caller-assigned Studio activity row. |
| Transaction id | Closed union: `txn_` followed by 26 uppercase Crockford Base32 characters for apply, or `undo_` followed by 26 such characters for undo. No other prefix is a transaction id. |
| Revision | JSON integer `1..2147483647`; proposal revisions begin at 1 and increase by exactly 1. |
| Concept id | Canonical bundle-relative concept id from the current spec, without leading slash or `.md`. |
| Content revision | Lower-case 64-hex SHA-256 of exact concept-file bytes; the absent sentinel is the literal `absent`. |
| Digest | `sha256:` plus lower-case SHA-256 over canonical JSON encoded as UTF-8. |
| Row token | 43-character unpadded base64url encoding of 32 random bytes. |
| Idempotency key | The shared contract for every keyed operation: callers **MUST generate at least 128 bits of entropy**; runtime cannot infer entropy from one observed value and therefore enforces only 32–128 visible ASCII bytes matching the closed full-string grammar `^[A-Za-z0-9._~-]+$`. Whitespace, non-ASCII, padding, alternate encoding, normalization, truncation, and endpoint-specific key alphabets are forbidden. HTTP headers, CLI options, and transport-neutral service calls delegate to the same structural validator. |
| Actor | Non-empty UTF-8 string of 1–128 code points, rendered as escaped text. |
| Runtime identity | `name@major.minor+sha256:<64-lower-hex>`; an implementation change that can alter output MUST change identity. |
| Repository relative path | UTF-8 string of 1–1,024 bytes matching `^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$`; leading `/`, `\`, NUL, empty segments, `.` and `..` segments, absolute paths, and non-canonical aliases are forbidden. |
| Stored byte size | JSON integer `0..67108864`, never a boolean; it is the exact regular-file `st_size` and must equal bytes read. |
| Canonical target identity | For an existing regular target, the closed object `{parent_device,parent_inode,leaf_device,leaf_inode,ctime_ns,mode,size,digest}`. Device, inode, `ctime_ns`, mode, and size are non-negative Linux `statx`/`fstat` integers (`ctime_ns` is the exact nanosecond value); `digest` hashes the exact regular-file bytes. The parent pair identifies the pinned containing directory and the leaf fields identify the opened file, not merely its pathname or content. |

Canonical JSON recursively sorts object keys, preserves array order, emits no insignificant whitespace, emits UTF-8 characters without ASCII escaping, and rejects NaN, infinity, and negative zero.
Digest fields are omitted from the object whose digest they define.
JSON nesting depth, counting the root as 1, MUST NOT exceed 32.
The caller supplies every stable `op_id`; the server validates format and uniqueness across the lineage and rejects reuse for a non-byte-identical canonical operation in a superseding revision.
The server assigns `proposal_id`, proposal revision, record ids, transaction ids, outbox ids, timestamps, sequences, and row tokens.

`validator_id`, `serializer_id`, and `operation_runtime_id` are persisted deliberately versioned identities, not package-version guesses.
`validator_id` identifies profile interpretation and finding fingerprints; `serializer_id` identifies parse/round-trip/byte serialization; `operation_runtime_id` identifies operation semantics and ordering.
They and `validation_context_digest` are authority-bearing inputs to snapshot, preview, read-model, pagination, and apply freshness digests.
`config_digest` remains the authority for effective parsed configuration semantics; `validation_context_digest` is separate evidence for the exact non-concept bytes, inventories, warnings, and frozen policy against which validation ran.

The checkout-local maintenance command
`python -B scripts/review_runtime_identities.py` prints deterministic JSON with
`current` and `candidate` identities for all three runtimes. It hashes each
`SOURCE_MANIFESTS` entry's exact bytes with SHA-256, then hashes canonical JSON
of the ordered `[relative_path, file_sha256]` pairs. Manifest array order is
significant; file metadata and checkout location are not. Optional
`--source-root /path/to/source-copy` reads bytes there using the invoking helper's
manifests; it does not import code or identities from that copy. Missing sources
fail without partial candidate output. The command writes no authority or source
files and does not approve its output. Production capture continues to use only
explicitly reviewed constants, never mutable-source hashing while serving.
The ordinary `tests/test_proposal_runtime_identity.py` gate rejects listed-source
drift; accepted identity changes require fresh preview/review, not rewriting old
persisted authority. Inventories require dependency review, not just digest
regeneration; candidate output does not certify complete dependency coverage.

Canonical target identity is stricter than content revision. Two leaves with the
same bytes but different inode, device, `ctime_ns`, mode, size, or pinned parent
identity are different authority. An expected-absent create target is represented
only by the existing `before_digest:"absent"` sentinel plus a descriptor-relative
absence proof under the same locks; it is not a fabricated all-null identity.
Where a regular before or resulting target is required, unavailable, partial, or
unknown identity is never replaced by digest-only evidence.
These device/inode identities are deliberately local authority for one running
Studio over one pinned bundle root. They are not portable review evidence across
machines, copied roots, remounted filesystems, or independent Studio sessions;
such a move requires a new capture and review.
Before transaction intent, a missing or changed required before identity is
`409 proposal_conflict` and creates no transaction authority. After prepared WAL
but before commit decision, an identity that cannot be safely restored is a
conflicted/corrupt rollback outcome with preserved evidence. At or after commit
decision, any required identity that is missing, unknown, or contradictory is
corruption and `indeterminate_outcome`.

## 4. Global limits

| Resource | Limit |
|---|---:|
| Create/supersede request body | 1,048,576 bytes before JSON parsing |
| Decision, withdraw, apply, undo request body | 65,536 bytes before JSON parsing |
| Summary | 1–240 code points |
| Rationale | 1–8,192 code points |
| Provenance entries / entry text | 16 / 2,048 code points |
| Operations / dependency edges | 128 / 512 |
| Affected concepts / snapshot concepts | 256 / 10,000 |
| Declared reads or writes per operation | 64 each |
| Decision reason | 0–1,024 code points |
| One concept before or after bytes | 2 MiB |
| Aggregate raw before+after bytes | 24 MiB |
| Preview artifact canonical serialized bytes, including base64 expansion | 36 MiB |
| Complete proposal read-model canonical serialized bytes | 40 MiB; creation rejects before persistence if exceeded |
| Aggregate unified diff | 6 MiB canonical UTF-8 and 150,000 lines |
| Validation findings | 10,000 before proposal creation fails `preview_limit_exceeded` |
| List page `limit` | default 50; range 1–100 |
| JSON nesting depth | 32 |
| Extension value / aggregate per envelope | 16,384 / 65,536 canonical bytes |
| Active nonterminal proposals per session / lineage | 500 / 8 |
| Total proposal lineages per session | 10,000 |
| Create/supersede rate | 10 per minute per session, token bucket burst 3 |
| Concurrent preview workers | 2 per session; queue cap 8 |
| Proposal repository aggregate quota | 2 GiB |
| Checkpoint canonical serialized size | 64 MiB each |
| Tombstone authority | 100,000 rows or 64 MiB, whichever first; quota exhaustion blocks new mutations |
| Quarantine diagnostic copies | 256 MiB; marker records are never quota-pruned |
| Audit rows | 1,000,000 rows or 512 MiB after checkpoint compaction |
| Service mutation WAL intents | 1,024 nonterminal files; 100,000 retained files; 512 MiB aggregate |
| One service mutation WAL intent | 41 MiB canonical serialized bytes |
| One apply/undo finalization intent | 41 MiB canonical serialized bytes |
| List projection page | 40 MiB canonical serialized bytes; never truncate an item |

Limits are checked before expensive parsing, staging, hashing, or validation where possible.
Every transport adapter MUST enforce the route body cap on raw bytes before JSON decoding, duplicate-key detection, schema construction, capture, preview-worker admission, or repository access. The transport-neutral service repeats the canonical serialized-size check for already-decoded callers; this defense in depth does not relax the pre-parse adapter requirement.
The active-proposal, lineage-revision, total-lineage, and repository-byte limits are rechecked while holding the final repository append/rotation and lineage locks immediately before a service intent is prepared and again before its domain row is appended. Create/supersede admission uses one repository-owned token bucket with the §4 rate/burst values; preview execution uses at most two workers and eight queued jobs. Admission state may be conservatively reconstructed after restart, but restart MUST NOT grant a burst larger than three. List projection reads at most the requested 100 rows, bounds every decoded row and preview, and fails `507 page_projection_too_large` rather than truncating, omitting, or lazily expanding authority beyond the 40 MiB page cap.
Exceeding a limit fails the operation; output MUST NOT be silently truncated in an authority-bearing preview.
Typed quota failures are `429 create_rate_limited`, `429 preview_concurrency_limited`, `507 repository_quota_exceeded`, `507 checkpoint_quota_exceeded`, `507 tombstone_quota_exceeded`, `507 quarantine_quota_exceeded`, and `507 audit_quota_exceeded`. Bundle transaction-lock contention is retryable `503 bundle_busy`, distinct from `503 lock_unavailable` for other lock classes.
Quota pressure MUST NOT prune a nonterminal transaction, uncertain result, idempotency authority, corruption marker, referenced preview, or not-yet-expired minimum-retention artifact.

## 5. Versioned persisted and wire schemas

### 5.1 Proposal revision schema

Every field below is required; empty arrays are permitted only where stated.

| Field | Type and rule |
|---|---|
| `schema` | Integer constant `1`. |
| `record_id` | Server-assigned `rec_` id. |
| `creation_result_record_id` / `creation_request_fingerprint` | Endpoint-result linkage assigned before proposal append. |
| `proposal_id` | Canonical id. |
| `proposal_revision` | Monotonic integer. |
| `supersedes_revision` | `null` for revision 1; otherwise exactly `proposal_revision-1`. |
| `mode` | String constant `proposal`. |
| `author` | Actor. |
| `created_at` | Server timestamp; immutable. |
| `summary` / `rationale` | Bounded strings. |
| `affected_concepts` | Sorted unique array equal to the union of all operation read/write sets. |
| `expected_snapshot` | Snapshot object in §6. |
| `operations` | Non-empty array of operation-v1 objects, stored in ascending `op_id` order. |
| `preview_ref` | Exact `{schema:1,preview_digest,artifact_bytes}`; the digest addresses one external immutable preview artifact and `artifact_bytes` is its canonical serialized byte length. |
| `provenance` | Array of `{kind,value}`; `kind` is `comment`, `plan`, `workflow`, or `external`; value is 1–2,048 code points. |
| `expires_at` | Exact preview-byte expiry timestamp. |
| `extensions` | Namespaced extension object; may be empty. |

The server assigns record/proposal ids, revision, `created_at`, preview reference, and `expires_at`.
Create input supplies `schema`, `mode`, `author`, `summary`, `rationale`, a complete `expected_snapshot`, operations without derived preview fields, provenance, and extensions.
The server recaptures under §6 and requires identical snapshot digest and authority fields, then persists the server-captured object with its own `captured_at`; mismatch is `409 snapshot_changed` and persists nothing except a terminal idempotency error.
For supersession it additionally supplies existing `proposal_id` and `supersedes_revision`; the server assigns the next revision under the lineage lock.
The proposal aggregate also stores withdrawal rows; a revision is the proposal row followed by zero or one withdrawal row in the same proposal log/checkpoint authority.

### 5.2 Closed operation union

Every operation has exactly these common fields: `schema:1`, `op_id`, `kind`, `target`, `args`, `reads`, `writes`, `expected_revisions`, `depends_on`, and `extensions`.
`target` is a canonical concept id and MUST occur in `writes`.
`reads` and `writes` are sorted, unique, non-empty concept-id arrays; their union MUST equal the keys of `expected_revisions`.
Every expected revision is a 64-hex content revision; creation and deletion are not in v1, so `absent` is invalid in an operation.
Dependencies are sorted unique `op_id` values, cannot name self, must exist in the same revision, and must form a DAG.

| `kind` | Exact `args` object | Derived read/write rule |
|---|---|---|
| `set_frontmatter` | `{key,value}` where `key` matches `^[A-Za-z_][A-Za-z0-9_-]{0,63}$`, is not `type`, and `value` is a JSON scalar/list/object representable by the safe YAML serializer, ≤64 KiB canonical JSON | reads=`[target]`, writes=`[target]` |
| `link_add` | `{destination,label,relation,detail,allow_forward_reference}`; destination is a canonical concept id; label 1–240; relation is `null` or matches `^[a-z][a-z0-9_]{0,63}$`; detail is `null` or ≤1,024; `allow_forward_reference` MUST be `false` in v1 | reads/writes include target; reads additionally include destination; writes exactly target |
| `entity_add` | `{label,kind,entity_id}`; label 1–240; kind 1–128; entity_id is `null` or 1–240 | reads=`[target]`, writes=`[target]` |
| `replace_text` | `{old,new,occurrence}`; old is 1–524,288 code points; new 0–524,288; occurrence is integer `1..10000` or string `only`; exact body-only replacement | reads=`[target]`, writes=`[target]` |
| `update_section` | `{heading,body,mode}`; heading is 1–512 and includes optional ATX level prefix; body ≤524,288; mode is `replace` or `append`; target heading must resolve exactly once | reads=`[target]`, writes=`[target]` |

`set_frontmatter` covers tags by setting `tags` to a list and typed relations by setting `relations` to a complete value; it cannot modify `type` because changing concept identity semantics is outside v1.
`link_add` uses the current mutator's standard Markdown-link and optional relation behavior and MUST be idempotent when the identical link/relation already exists.
The protocol admits no create, delete, rename, asset, shell, argv, plugin, Python, JavaScript, network, package, or arbitrary-file operation.

### 5.3 Snapshot, preview, validation, and diff schemas

The expected snapshot is:

```json
{
  "schema": 1,
  "captured_at": "2026-07-14T12:34:56.789Z",
  "concepts": [{"concept":"reference/spec","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","identity":{"parent_device":1,"parent_inode":2,"leaf_device":1,"leaf_inode":3,"ctime_ns":1784032496789000000,"mode":33188,"size":4096,"digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}}],
  "capabilities": ["okf.cap.typed_relations"],
  "config_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "validation_context_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "validator_id": "okf-validator@1.0+sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "serializer_id": "okf-roundtrip@1.0+sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "operation_runtime_id": "proposal-ops@1.0+sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "snapshot_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

`concepts` is the complete §5-visible concept inventory sorted by concept id.
Every entry is the closed object `{concept,revision,identity}` where `identity`
is the exact local canonical target identity from §3 and its `digest` equals
`revision`. The inventory therefore binds both reviewed bytes and the precise
parent/leaf object that supplied them; same bytes installed at a different
parent, device, inode, `ctime_ns`, mode, or size are a different snapshot.
`capabilities` is the sorted active capability-id set.
`config_digest` hashes the effective parsed configuration relevant to scanning, parsing, serialization, validation, and capabilities.
`validation_context_digest` hashes canonical JSON of `{schema:1,reserved_files,visible_non_concept_files,scan_controls,load_warnings,validation_policy}` captured under §6. `reserved_files` is the sorted canonical path map for visible reserved `index.md` and `log.md` files, with each value `{revision,size,bytes_base64}` over exact captured bytes. `visible_non_concept_files` is the sorted inventory required by the frozen validator for asset existence and index checks, with each entry `{path,kind,size,revision}`, where `kind` is `asset` or `validation_input`, and no host-derived metadata. `.okf-loom-bundle.lock` and `*.okf-lock` are implementation ownership artifacts and MUST be absent from this inventory and every digest input. `scan_controls` is the sorted canonical path map of every visible file or manifest whose bytes control scan visibility, each with `{revision,size,bytes_base64}`. `load_warnings` contains closed `{code,path,line,message,fingerprint}` objects sorted by the finding rule; `path` is a canonical captured bundle-relative path or `null` only for a bundle warning, and `line` follows the finding line rule. `validation_policy` is exactly `{profile,policy_id,rules}`, where `policy_id` is a runtime identity over the complete profile/rule value, `rules` is sorted by unique `code`, and each closed rule is `{code,base_severity,scope,severity,fails_profile,parameters}` with `scope` one of `bundle`, `reserved_file`, `config`, `capability`, or `concept`, `severity` the selected profile's effective severity, and `parameters` a canonical JSON object. It is the sole normalization authority used for both before and after validation; missing, duplicate, unknown, or identity-mismatched rules fail closed. Paths are bundle-relative canonical paths; revisions hash exact bytes; arrays use the stated order. This digest does not replace parsed configuration semantics in `config_digest`.
`snapshot_digest` hashes `{schema,concepts,capabilities,config_digest,validation_context_digest,validator_id,serializer_id,operation_runtime_id}`.

Preview-v1 is a content-addressed artifact containing exactly `schema`, `proposal_id`, `proposal_revision`, `snapshot_digest`, `validation_context_digest`, `validator_id`, `serializer_id`, `operation_runtime_id`, `operation_order`, `concept_changes`, `diff`, `validation`, `generated_at`, `expires_at`, and `preview_digest`.
`operation_order` is canonical topological order.
Each concept change is `{concept,before_revision,after_revision,before_identity,before_base64,after_base64,contributing_ops}` with exact bytes encoded standard padded base64 and contributing op ids sorted. `before_identity` is the exact matching snapshot identity for an existing concept and is null only when `before_revision` is `absent` and the capture proved descriptor-relative absence. The preview, its digest, and every review projection therefore retain exact local file identity rather than digest-only authority.
Diff is `{schema:1,format:"unified",context_lines:3,text,digest}` with paths `a/<concept>.md` and `b/<concept>.md`, LF diff-control lines, and original content line endings preserved inside hunk content.
Validation is `{schema:1,profile,validation_context_digest,before_findings,after_findings,added,removed,unchanged,status,digest}`.
`profile` is exactly the explicitly requested installed profile; v1 defaults to `producer` and MUST NOT silently fall back.
Findings use `{code,severity,concept,line,message,fingerprint}`. `concept` MAY be `null` only when the frozen validation policy classifies the finding as bundle-, reserved-file-, configuration-, or capability-scoped; every concept-scoped finding MUST carry its canonical concept id. `line` is a non-negative integer and `0` means that no meaningful source line exists; positive values are one-based source lines. Finding scope comes from the frozen rule definition, never from message text. Arrays use the null-safe canonical sort `(0 if concept is null else 1, concept-or-empty-string, line, severity, code, fingerprint)`, so bundle-level findings sort first.
`fingerprint` hashes canonical `{code,severity,concept,line,message}` and therefore preserves null ownership and `line:0`; `validation.digest` hashes the complete validation object excluding only `digest`, including `validation_context_digest` and every ordered finding array.
`status` is `pass` only when the staged snapshot satisfies that profile; warnings do not fail unless the profile says they fail.
`preview_digest` hashes the complete preview excluding itself.
The immutable artifact path and bytes are the sole preview authority.
The proposal row carries only `preview_ref`; API read models may embed the artifact for transport but that embedding is a projection and MUST exactly verify `preview_ref.preview_digest` and `artifact_bytes` first.
Missing, non-regular, oversized, mismatched, unsupported-runtime, or checksum-invalid preview artifacts leave the persisted lifecycle state unchanged but make its read model unavailable, disable decisions/apply, and return `503 preview_authority_unavailable`; no inline or cached copy may substitute.
Apply rechecks every snapshot and preview `before_identity` under the complete
preflight lock envelope. If the reviewed bytes were replaced by another local
file object, even with the same revision digest, apply returns pre-WAL
`409 proposal_conflict` with conflict code `target_identity_changed`; it writes
no intent, history, WAL, endpoint result, audit, stage, or backup authority.

### 5.4 Decision rows and tokens

Decision rows are append-only full records with exactly:

```json
{
  "schema": 1,
  "record_id": "rec_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
  "decision_result_record_id": "rec_01J2Y8N5Z9Q3A7K6M4T1V0WXY0",
  "decision_request_fingerprint": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "proposal_id": "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
  "proposal_revision": 1,
  "op_id": "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
  "decision": "accepted",
  "reason": "",
  "actor": "local-user",
  "decided_at": "2026-07-14T12:40:00.000Z",
  "previous_row_token": null,
  "row_token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
}
```

`decision` is only `accepted` or `rejected`.
`reason` is optional in intent but serialized as a string; it MUST be non-empty when rejecting an operation with direct or transitive dependents.
The first decision requires `previous_row_token:null`; replacement requires the exact current row token.
The decision-set token is the SHA-256 digest of canonical sorted tuples `[op_id,current_row_token-or-null]` for every operation.
Changing any decision changes the token.
`actor` is an unauthenticated local label and conveys no principal or authorization.

### 5.5 Conflict schema

A conflict is a derived closed object `{schema,code,proposal_id,proposal_revision,op_id,concept,expected,actual,message,detected_at}`.
`op_id` and `concept` may be `null` only for proposal-level conflicts.
`code` is exactly one of `snapshot_changed`, `read_revision_changed`, `write_revision_changed`, `target_missing`, `target_type_changed`, `target_identity_changed`, `capability_changed`, `config_changed`, `preview_expired`, `decision_stale`, `dependency_unsatisfied`, or `validation_stale`.
Conflicts sort by `(op_id-or-empty,concept-or-empty,code)` and never mutate stored proposal or decisions.

### 5.6 Apply/undo requests, durable finalization intent, public results, and complete transaction WAL

Apply request-v1 has exactly `schema`, `proposal_id`, `proposal_revision`, `actor`, `snapshot_digest`, `validation_context_digest`, `decision_set_token`, and `read_model_token`.
The `Idempotency-Key` HTTP header or `--idempotency-key` CLI option is REQUIRED and is not duplicated in the body.
The request fingerprint is the digest of exact route scope, canonical body, repository id, protocol version, and relevant runtime identities; it excludes the CSRF token and unauthenticated actor label as principal authority.

Undo request-v1 has exactly `schema`, `proposal_id`, `proposal_revision`, `actor`, `transaction_id`, `undo_group_id`, and `read_model_token`.

Public transaction result-v1 contains exactly `schema`, `operation`, `transaction_id`, `proposal_id`, `proposal_revision`, `status`, `replayed`, `decision_set_token`, `validation_context_digest`, `validation_digest`, `concept_revisions`, `activity_ids`, `undo_group_id`, `completed_at`, `undo_expires_at`, and `error`.
For apply, status is exactly `applied`; for undo it is exactly `undone`.
These are HTTP `200` successes and have `error:null`, non-empty revisions, completion time, and activity ids.
Every non-success is an HTTP error envelope, not a result body; its persisted endpoint result stores the exact error envelope and status so replay cannot reinterpret failure.

Every apply/undo transaction has one immutable closed finalization intent at
`transactions/<transaction_id>/intent.json`. It is the sole authority for
creating transaction audit, activity, outbox, and endpoint-result records after
a crash; the WAL is progress authority and MUST NOT be used as a recipe for
those records. The intent is assigned completely, written through exclusive
temporary creation, fsynced, atomically renamed, its transaction directory
fsynced, reread, checksum-verified, and semantic-linkage-verified before
`history.json` is published and before a `prepared` WAL may exist.

Transaction-finalization-intent-v1 contains exactly these fields:

| Field | Exact rule |
|---|---|
| `schema` | Integer constant `1`. |
| `transaction_id`, `operation`, `parent_transaction_id` | Canonical transaction identity; operation is `apply` or `undo`; parent is null for apply and the exact applied transaction id for undo. |
| `proposal_id`, `proposal_revision`, `actor` | Exact immutable proposal and caller-label authority accepted by preflight. |
| `endpoint` | Closed `{scope,key_digest,request_fingerprint,result_record_id}` with the exact route-template scope, idempotency-key digest, request fingerprint, and preassigned endpoint-result `rec_` id. |
| `accepted_authority` | Closed `{read_model_token,read_revisions,snapshot_digest,decision_set_token,preview_digest,validation_context_digest,validator_id,serializer_id,operation_runtime_id}`. `read_revisions` is the canonical concept-id map actually accepted under lock (the accepted-operation read union for apply; every current target for undo), and `snapshot_digest` is the exact accepted complete bundle snapshot for either operation. Apply values are otherwise the exact proposal preflight authorities. For undo, only `decision_set_token` and `preview_digest` are null; every other value is non-null. |
| `accepted_op_ids` | Sorted exact effective dependency-closed accepted set for apply; empty for undo. It contains only operations whose derived `effective_state` is `accepted`, never every row whose explicit decision says `accepted`. |
| `decision_record_ids`, `decision_rows_digest` | Sorted exact decision row ids and digest of their complete canonical rows for apply; empty array and null for undo. |
| `validation` | Closed `{profile,digest,status}`; exact final staged validation profile/digest/status. Because intent publication follows successful final validation, status is `pass`; validation failure writes no transaction intent. |
| `candidate_ids` | Closed `{commit,rollback}`; each branch is `{audit_record_id,activity_ids,outbox_ids,result_record_id}`. Arrays are sorted and unique; commit arrays are non-empty where that operation emits the row kind; rollback arrays may be empty. Both branch result ids exactly equal `endpoint.result_record_id`, because only one mutually exclusive branch may become terminal. |
| `candidates` | Closed `{commit,rollback}`; each branch is `{audit,activities,outbox,result}` containing complete closed payloads or the exact templates defined below. Every id agrees with the corresponding branch in `candidate_ids`. Commit describes only `applied` or `undone`; rollback describes only the pre-decision terminal error. Recovery selects commit only from a verified WAL with `commit_decided:true`, otherwise rollback only from a verified pre-decision rollback phase; it never merges branches. |
| `undo_group_id` | Preassigned immutable group id; for undo, exactly the group of the parent apply. |
| `timestamps` | Closed `{started_at,commit_decided_at,commit_completed_at,rollback_completed_at,undo_expires_at,audit_expires_at,replay_expires_at}`. All values are preassigned transaction-logical canonical instants and MUST NOT be described as later wall-clock observations; `commit_decided_at` and `commit_completed_at` are used only by commit candidates, rollback audit/result use null commit time and exact `rollback_completed_at`, and `undo_expires_at` is null only when undo is unavailable. Candidate payloads repeat these exact values; timestamps are never placeholders. |
| `retention` | Closed `{audit_until,replay_until,undo_until}` repeating the applicable timestamps above; nullability is identical. |
| `history` | Closed `{relative_path,manifest_digest}`; path is exactly `transactions/<transaction_id>/history.json` and the digest is over the exact immutable history bytes that must be published next. The manifest is prepared authority binding every concrete before identity, staged authority, and backup identity; transaction WAL and audit, not a mutable manifest field or replacement manifest, govern lifecycle and resulting identities. |
| `targets` | Canonical concept-order array of `{concept,before_digest,after_digest,before_identity,staged_identity,resulting_identity,backup_identity}`. `before_identity` is the exact canonical target identity captured under bundle-exclusive and concept locks, or null only when `before_digest` is `absent` and descriptor-relative absence was proved. `staged_identity` and backup identity are complete private regular-file identities. In the immutable intent, `resulting_identity` is the whole-value `target_resulting_identities` placeholder because rename changes `ctime_ns`; substitution may use only exact post-publication identities from the verified WAL. All values exactly match history and WAL target linkage. |
| `allowed_placeholders` | Sorted unique array containing only placeholder names actually present in candidate templates. |
| `checksum` | Digest of the canonical intent excluding only `checksum`; `transaction_intent_digest` is the digest of the complete canonical intent bytes including this verified checksum. Candidate checksum placeholders remain placeholders in both byte sequences: no candidate checksum participates in the intent checksum or intent digest as a concrete value. |

Candidate values are either complete closed payloads or templates that are
otherwise byte-for-byte complete. A placeholder is represented only as the
whole JSON value `{"$intent_placeholder":"<name>"}`; it cannot occur inside a
string, key, array element mixed with other data, extension, id, actor,
or timestamp. Digest/checksum fields accept a placeholder only in the explicit
positions below. The only allowed names are
`target_before_revisions`, `target_after_revisions`,
`target_before_identities`, `target_resulting_identities`,
`target_backup_identities`, `resulting_revisions`, and
`concept_revisions`, plus `transaction_intent_digest` and
`candidate_checksum`. `transaction_intent_digest` may occur only in the
transaction-audit field of that name. `candidate_checksum` may occur only at
`candidates.commit.audit.checksum`, `candidates.rollback.audit.checksum`, or
the `checksum` value of a templated endpoint result or outbox receipt. At each
of those paths it denotes the checksum of the containing candidate, not a
checksum copied from mutable or regenerated state.
`allowed_placeholders` MUST equal the sorted set used by all templates;
unknown, unused, duplicated, placeholder-containing-placeholder, wrongly
positioned, or partially embedded placeholders make the intent invalid.
The `target_resulting_identities` placeholder additionally appears as the whole
`resulting_identity` value in each intent target and in candidate audit/result
templates. It never appears in immutable `history.json` and may be closed only
from exact verified WAL identities after publication.

Final substitution is pure, deterministic, and acyclic. Recovery performs the
following steps in order and MUST NOT reorder or merge them:

1. Verify the intent `checksum`; compute and verify the complete canonical
   `transaction_intent_digest`; verify that digest against the WAL together
   with operation and transaction identity; and verify every WAL target
   against the intent and history. Candidate checksum positions still contain
   their exact placeholder objects during both intent hashes.
2. Derive the target values only from the verified ordered WAL targets:
    before/after revision maps use `before_digest`/`staged_digest` respectively;
    before and resulting identity maps use the exact `before_identity` and
    post-publication `resulting_identity` objects respectively;
   the backup map uses the exact verified backup identities;
   `resulting_revisions` is the after map for apply and the `restore_digest`
   map for undo; and `concept_revisions` equals `resulting_revisions`. Substitute
   those whole placeholders.
3. Substitute the verified `transaction_intent_digest` into the selected
   candidate audit's transaction-audit field.
4. Compute that audit's `candidate_checksum` over its canonical bytes excluding
   only `audit.checksum`, then substitute the result at the selected branch's
   `audit.checksum` path.
5. Validate and canonicalize the now-closed audit, including every cross-link,
   and derive any audit digest from those exact closed canonical bytes.
6. Use only that resulting closed audit digest/checksum for any declared
   endpoint or outbox binding substitutions. Process the remaining dependency
   graph in topological order, computing an endpoint-result or outbox
   `candidate_checksum` only after all of that containing candidate's upstream
   substitutions and excluding only its own `checksum`. No endpoint or outbox
   value may feed back into the audit, intent checksum, or intent digest.

All other candidate digest/checksum fields are immutable values precomputed for
the post-substitution bytes and MUST verify exactly. Recovery then validates
each resulting closed row schema and all cross-links, canonicalizes it, and
appends those exact bytes. No clock, current proposal projection, mutable
decision state, regenerated notification, inferred id, or other live input may
participate in finalization.

An activity candidate is exactly
`{type:"activity",id,actor,origin:"proposal",action,ids,summary,undoable,detail}`;
`id` is its assigned `act_` id, `action` is `apply` or `undo`, `ids` is the
canonical concept-id array, and closed `detail` contains exactly
`{transaction_id,proposal_id,proposal_revision,undo_group_id}`. The existing
Studio event store may add only its normal `event_id`, `ts`, `seq`,
`event_cursor`, and `rev` envelope fields after append. Those five fields are
the complete enumeration of later-derived activity fields; they are not
finalization placeholders or recovery authority. An outbox candidate is the
exact pending receipt; only its already-defined delivery fields `state`,
`event_cursor`, `attempts`, and `updated_at` may later advance under §5.7.

Compact intent-shape example (ellipses are explanatory notation, not JSON, and
the focused fragment below supplies the complete checksum-placeholder rule):

```json
{"schema":1,"transaction_id":"txn_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ","operation":"apply","parent_transaction_id":null,"proposal_id":"01J2Y8N5Z9Q3A7K6M4T1V0WXYZ","proposal_revision":1,"actor":"local-user","endpoint":{"scope":"POST /__proposal/v1/proposals/{id}/revisions/{rev}/apply","key_digest":"sha256:…","request_fingerprint":"sha256:…","result_record_id":"rec_01J2Y8N5Z9Q3A7K6M4T1V0WXY0"},"accepted_authority":{"read_model_token":"sha256:…","read_revisions":{"tables/orders":"sha256:…"},"snapshot_digest":"sha256:…","decision_set_token":"sha256:…","preview_digest":"sha256:…","validation_context_digest":"sha256:…","validator_id":"okf-validator@1.0+sha256:…","serializer_id":"okf-roundtrip@1.0+sha256:…","operation_runtime_id":"proposal-ops@1.0+sha256:…"},"accepted_op_ids":["op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"],"decision_record_ids":["rec_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"],"decision_rows_digest":"sha256:…","validation":{"profile":"producer","digest":"sha256:…","status":"pass"},"candidate_ids":{"commit":{"audit_record_id":"rec_01J2Y8N5Z9Q3A7K6M4T1V0WXX1","activity_ids":["act_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"],"outbox_ids":["out_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"],"result_record_id":"rec_01J2Y8N5Z9Q3A7K6M4T1V0WXY0"},"rollback":{"audit_record_id":"rec_01J2Y8N5Z9Q3A7K6M4T1V0WXX2","activity_ids":[],"outbox_ids":[],"result_record_id":"rec_01J2Y8N5Z9Q3A7K6M4T1V0WXY0"}},"candidates":{"commit":{"audit":{"schema":1,"record_id":"rec_01J2Y8N5Z9Q3A7K6M4T1V0WXX1","transaction_intent_digest":{"$intent_placeholder":"transaction_intent_digest"},"resulting_revisions":{"$intent_placeholder":"resulting_revisions"},"…":"complete closed audit fields"},"activities":[{"…":"complete closed activity payload"}],"outbox":[{"…":"complete closed receipt payload"}],"result":{"…":"complete closed endpoint-result payload"}},"rollback":{"audit":{"…":"complete closed rollback audit fields"},"activities":[],"outbox":[],"result":{"…":"complete closed rollback endpoint result"}}},"undo_group_id":"undo_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ","timestamps":{"started_at":"2026-07-14T12:40:00.000Z","commit_decided_at":"2026-07-14T12:40:00.001Z","commit_completed_at":"2026-07-14T12:40:00.002Z","rollback_completed_at":"2026-07-14T12:40:00.003Z","undo_expires_at":"2026-08-13T12:40:00.002Z","audit_expires_at":"2027-01-10T12:40:00.002Z","replay_expires_at":"2026-07-21T12:40:00.002Z"},"retention":{"audit_until":"2027-01-10T12:40:00.002Z","replay_until":"2026-07-21T12:40:00.002Z","undo_until":"2026-08-13T12:40:00.002Z"},"history":{"relative_path":"transactions/txn_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ/history.json","manifest_digest":"sha256:…"},"targets":[{"concept":"tables/orders","before_digest":"sha256:…","after_digest":"sha256:…","backup_identity":{"relative_name":"orders.before","device":1,"inode":2,"mode":384,"size":128,"digest":"sha256:…"}}],"allowed_placeholders":["resulting_revisions","transaction_intent_digest"],"checksum":"sha256:…"}
```

For avoidance of doubt, the compact line above abbreviates the identity amendment:
its displayed `history.manifest_digest` is the normative immutable history digest,
and each displayed target additionally contains the normative `before_identity`,
`staged_identity`, and whole-value `target_resulting_identities` placeholder. Its
`allowed_placeholders` likewise includes `target_resulting_identities`. The field
tables and substitution rules, not omission in this compact display, are authority.

The audit/checksum portion abbreviated by the ellipses above is normatively
equivalent to this focused template fragment for both branches (unselected
fields are omitted only for display):

```json
{"candidates":{"commit":{"audit":{"transaction_intent_digest":{"$intent_placeholder":"transaction_intent_digest"},"resulting_revisions":{"$intent_placeholder":"resulting_revisions"},"checksum":{"$intent_placeholder":"candidate_checksum"}}},"rollback":{"audit":{"transaction_intent_digest":{"$intent_placeholder":"transaction_intent_digest"},"checksum":{"$intent_placeholder":"candidate_checksum"}}}},"allowed_placeholders":["candidate_checksum","resulting_revisions","transaction_intent_digest"]}
```

The intent checksum and `transaction_intent_digest` hash those literal
`candidate_checksum` placeholder objects. Recovery substitutes the intent
digest, computes and closes the selected audit checksum, and only then resolves
audit-bound endpoint/outbox fields and their own checksums. A fragment with a
concrete commit or rollback audit checksum while computing the intent digest is
invalid because it recreates the cryptographic cycle.

An intent missing a candidate payload, carrying a candidate id that does not
match its payload, admitting a timestamp or id placeholder, or disagreeing
with WAL/history is invalid and is never completed by inference.

The apply/undo WAL is one closed discriminated schema with exactly these fields:

| Field | Exact rule |
|---|---|
| `schema` | Integer constant `1`. |
| `operation` | `apply` or `undo`. |
| `transaction_id` | Server id with matching operation prefix. |
| `proposal_id`, `proposal_revision` | Exact proposal authority. |
| `idempotency_scope`, `idempotency_key_digest`, `request_fingerprint` | Scope is exact route template; digests are canonical SHA-256. |
| `transaction_intent_digest` | Required immutable digest of the operation-specific verified `intent.json`; it never changes across WAL phases. |
| `decision_set_token`, `read_model_token`, `preview_digest`, `snapshot_digest`, `validation_context_digest` | Authority at prepare; nullable only for undo fields not applicable. |
| `validator_id`, `serializer_id`, `operation_runtime_id` | Exact persisted runtime identities. |
| `accepted_op_ids` | The same sorted exact effective dependency-closed accepted set as intent; empty for undo. Any difference from intent is invalid WAL. |
| `targets` | Ordered target array described below. |
| `stage_dir_identity`, `backup_dir_identity` | `{relative_name,device,inode,mode}` for private pinned directories. |
| `phase` | `prepared`, `commit_decided`, `publishing`, `content_published`, `finalizing`, `committed`, `rolling_back`, `rolled_back`, or `indeterminate`. |
| `commit_decided` | Boolean; false only in `prepared`, `rolling_back`, `rolled_back`; true in every forward phase; either value only in `indeterminate` reflecting last verified durable value. |
| `publish_bitmap`, `rollback_bitmap` | Arrays of booleans exactly target length. |
| `recovery_attempts` | Integer `0..100`; reaching 100 enters `indeterminate` and requires operator repair. |
| `created_at`, `updated_at` | Server timestamps. |
| `checksum` | Digest of canonical record excluding checksum. |

Each ordered target is exactly `{concept,before_digest,staged_digest,backup_digest,restore_digest,before_identity,staged_identity,resulting_identity,stage_leaf,backup_leaf}`.
`restore_digest` equals `before_digest` for apply and the retained pre-apply digest for undo; all digests describe exact bytes.
`stage_leaf` and `backup_leaf` are exclusive private regular-file identities `{relative_name,device,inode,mode,size,digest}`.
`before_identity` is the canonical target identity from §3, with the expected-absent exception defined there. `staged_identity` is the exact source identity captured before publication and includes `{parent_device,parent_inode,leaf_device,leaf_inode,ctime_ns,mode,size,digest}`. `resulting_identity` is null only while the target's publish bit is false; immediately after exchange/rename it MUST be the complete canonical target identity observed at the destination. A true publish bit with null/partial identity, or a false bit with a claimed resulting identity, is invalid WAL. Undo targets additionally bind the parent apply's exact `resulting_identity` as their required current `before_identity`; digest equality alone is insufficient.
Target order is canonical UTF-8 concept-id order and all bitmaps use that order.

Legal phase/bitmap combinations are closed:

| Phase | `commit_decided` | Publish bitmap | Rollback bitmap |
|---|---:|---|---|
| `prepared` | false | all false | all false |
| `commit_decided` | true | all false | all false |
| `publishing` | true | non-empty proper prefix true | all false |
| `content_published` | true | all true | all false |
| `finalizing` | true | all true | all false |
| `committed` | true | all true | all false |
| `rolling_back` | false | all false | any non-empty true suffix in canonical target order (the completed prefix of reverse-target rollback order), including all true |
| `rolled_back` | false | all false | all true |
| `indeterminate` | last durable value | any verified prefix only | any verified reverse prefix only; never overlapping true bits |

The closed forward transition graph is cardinality-sensitive. For exactly one
target, `commit_decided → content_published` is legal only after that target's
descriptor-relative exchange/rename, immediate and repeated exact source and
destination identity verification, parent-directory fsync, and durable atomic replacement of
the WAL with phase `content_published` and publish bitmap `[true]`; a
`publishing` record is neither required nor legal for this transaction. For
two or more targets, `commit_decided → publishing` is required after the first
such publication/fsync/identity verification, every persisted `publishing`
bitmap MUST be a non-empty proper prefix, and only
`publishing → content_published` may durably replace the final proper-prefix
state with the all-true bitmap after the final target passes the same checks.
Thus `commit_decided → content_published` is illegal for every multi-target
transaction, even if all canonical bytes happen to match their staged digests.

Pre-decision rollback has the same durable-prefix discipline in reverse target
order. After each restored target, including the final target of a multi-target
transaction, the coordinator MUST durably replace and verify `rolling_back`
with the next reverse-prefix bitmap. In particular, it MUST persist and verify
`rolling_back` with an all-true rollback bitmap before it may write
`rolled_back` or append/finalize rollback audit/result authority. A direct
proper-prefix-to-`rolled_back` transition is invalid.

Every WAL advance writes a complete replacement to an exclusive temporary, fsyncs it, atomically renames it, fsyncs the WAL parent, rereads, and verifies checksum, phase monotonicity, legal bitmaps, and exact identity lifecycle before the next side effect.
Every advance also revalidates the same intent digest and transaction/operation
association. Apply and undo schemas reject the other's nullability, candidate
status, accepted-authority, parent, and target-result rules; an intent cannot be
reinterpreted as the other operation.
The durable `commit_decided` phase MUST be written and verified before the first canonical exchange/rename.
Before `commit_decided`, recovery rolls back or leaves canonical bytes unchanged.
At or after `commit_decided`, recovery MUST roll forward.
`content_published` means all canonical publication completed and verified; it does not mean commit intent.

### 5.6A Transport-neutral service mutation WAL

Create, supersede, keyed or keyless decision, and withdrawal use a repository-owned service mutation WAL distinct from the apply/undo transaction WAL in §5.6. This WAL records metadata publication intent; it never authorizes concept-byte publication, apply, or undo. The repository MUST durably write and verify one `prepared` service intent before appending a proposal, decision, withdrawal, outbox, endpoint result, or other terminal row for that mutation.

The closed service-intent-v1 object contains exactly these fields:

| Field | Exact rule |
|---|---|
| `schema` | Integer constant `1`. |
| `intent_id` | Server-assigned `svc_` id; immutable and globally unique in the repository. |
| `operation` | Exactly `create`, `supersede`, `decision`, or `withdraw`. |
| `scope`, `key_digest`, `request_fingerprint` | Exact service method/route-template scope and canonical digests. A generated keyless-decision identity still has a repository-private key digest. |
| `repository_id` | Exact owning repository identity. |
| `proposal_id`, `proposal_revision`, `op_id` | Assigned or addressed immutable domain identity; `op_id` is non-null only for `decision`. |
| `base_proposal_revision` | `null` for create; the exact current predecessor for supersede; the addressed revision for decision/withdraw. |
| `accepted_repository_snapshot_token` | Exact immutable repository snapshot token observed before speculative work. Final admission rechecks current disk authority under lock; when unrelated authority advanced, proposal-specific revalidation must still prove the accepted proposal view unchanged. |
| `accepted_read_model_token` | `null` for create/supersede; for decision/withdraw, the exact mutable CAS token accepted under the final lock. |
| `assigned` | Closed object `{record_id,result_record_id,outbox_id,created_at,domain_at,expires_at}`. Each id/timestamp is assigned before `prepared`; `domain_at` is the exact `created_at`, `decided_at`, or `withdrawn_at` embedded in the domain row. |
| `domain` | Closed object `{aggregate,kind,row,row_digest}`. Aggregate/kind is exactly `proposals/proposal`, `proposals/withdrawal`, or `decisions/decision`; `row` is the exact closed immutable row and `row_digest` hashes it. |
| `notification_payload` | Exact immutable closed proposal notification object from §5.7, not a recipe or state reference. |
| `outbox` | Exact closed outbox receipt from §5.7, including the same payload and its binding to this intent/result. |
| `terminal_http_status`, `terminal_response`, `terminal_response_digest` | Exact eventual HTTP status and closed domain/HTTP success or error envelope. Status is `100..599`; response digest hashes the exact canonical envelope. Transport-neutral callers return the same domain envelope even when they do not expose HTTP. |
| `preview` | For create/supersede, exact `{staged_ref,promoted_ref}`; for decision/withdraw, `null`. A staged reference is `{relative_path,preview_digest,artifact_bytes}` and a promoted reference is the proposal `preview_ref`; both identify the same bytes. |
| `phase` | Exactly `prepared`, `domain_appended`, `outbox_appended`, `terminal`, `committed`, or `indeterminate`. |
| `recovery_attempts` | Integer `0..100`; attempt 100 becomes `indeterminate`. |
| `created_at`, `updated_at` | Exact server timestamps; `created_at` equals `assigned.created_at`. |
| `checksum` | Digest of canonical record excluding only `checksum`. |

All nested objects above are closed. The complete `domain.row`, `notification_payload`, `outbox`, and `terminal_response` bytes are intent authority; recovery MUST NOT reconstruct any of them from current mutable proposal state. Their internal ids, timestamps, proposal revision, preview digest, read-model token, and result link MUST mutually agree. An inconsistency, changed duplicate, unsupported phase, skipped phase, checksum failure, or phase whose required append cannot be proved creates a corruption marker and makes the intent `indeterminate`; it never permits a fresh mutation.

Legal progress is the listed phase order. `prepared` proves only durable intent and assigned values. `domain_appended` proves the exact domain row is durably present. `outbox_appended` proves the exact pending receipt is durably present. `terminal` proves the exact endpoint result is durably present. `committed` proves all four authorities mutually verify. Each advance is a complete atomic replacement using temporary-file fsync, rename, parent-directory fsync, reread, checksum verification, and monotonic-phase verification before the next append.

Recovery rolls forward the exact stored rows by immutable identity and digest. Existing identical rows count as completed; absence appends the intent bytes; any different row with the same identity is corruption and indeterminate. A prepared endpoint result without one coherent matching service intent is never treated as unused idempotency authority and never starts a fresh mutation: recovery writes an indeterminate result/tombstone where safe, otherwise a corruption marker, and all same-key retries return `indeterminate_outcome` until explicit repair. A request waiter, process lifetime, transport connection, or in-memory cache has no authority over this protocol.

For decision and withdrawal, `request_fingerprint` is computed from protocol version, exact scope, repository id, addressed immutable identities, relevant runtime identities, and the canonical request with `read_model_token` omitted. The token is mutable CAS evidence, not idempotent intent identity. The terminal service intent nevertheless binds `accepted_read_model_token` to the exact token accepted under lock. On every same-scope/key retry, endpoint result/tombstone/service-intent reconciliation occurs before current token or stale-token evaluation. A different non-token fingerprint conflicts; the same fingerprint resumes or replays the original accepted-token outcome even when the current read-model token has since changed.

### 5.7 Checkpoints, notifications, and legacy projection

Every checkpoint is `{schema:1,repository_id,kind,generation,sequence_high_water,covered_generations,records_digest,records,created_at,checksum}`.
`kind` is `proposals`, `decisions`, `transactions`, `endpoint_results`, `outbox`, `tombstones`, or `corruption`.
`generation` is an unsigned integer increasing by one.
`checksum` hashes the canonical object excluding `checksum`; `records_digest` hashes the canonical `records` array.

Proposal SSE notification payload is exactly `{schema:1,proposal_id,proposal_revision,event,read_model_token,transaction_id}`.
`event` is `created`, `superseded`, `decision_changed`, `withdrawn`, `conflict_changed`, `applied`, `undo_changed`, or `expired`; `transaction_id` is nullable.
It is notification only and contains no canonical proposal state.

A legacy suggestion projection is exactly `{schema:1,kind:"legacy-suggestion",legacy_identity:{legacy_id,source_generation},summary,author,created_at,regeneration_required:true,applicable:false}`.
Malformed complete legacy rows are quarantined from projection and reported as storage corruption; they are never guessed into shape.

Every persisted JSONL row uses the exact wrapper `{record_schema:1,kind,sequence,payload,checksum}`.
`kind` is `proposal`, `withdrawal`, `decision`, `transaction`, `endpoint_result`, `outbox`, `tombstone`, `corruption`, or `repair`; `sequence` is a strictly increasing unsigned integer within that aggregate; `payload` is the corresponding closed object; and `checksum` hashes the canonical wrapper excluding `checksum`.
`repair` rows share the `corruption` aggregate, sequence, generations, and checkpoint; there is no independent repair log.
The wrapper is storage-only and never appears in public wire responses.

A withdrawal payload is exactly `{schema:1,record_id,withdrawal_result_record_id,withdrawal_request_fingerprint,proposal_id,proposal_revision,actor,reason,read_model_token,withdrawn_at}`; reason is 1–1,024 code points.
Apply and undo transaction audit rows share the `transactions` aggregate/checkpoint and are discriminated by `operation`.
Withdrawal rows share the `proposals` aggregate/checkpoint.

A transaction audit payload is exactly `{schema:1,record_id,operation,transaction_id,parent_transaction_id,proposal_id,proposal_revision,actor,endpoint_scope,idempotency_key_digest,request_fingerprint,transaction_intent_digest,accepted_op_ids,decision_record_ids,decision_rows_digest,decision_set_token,preview_digest,snapshot_digest,validation_context_digest,validator_id,serializer_id,operation_runtime_id,validation_profile,validation_digest,validation_status,phase,status,history_manifest_digest,target_before_revisions,target_after_revisions,target_before_identities,target_resulting_identities,target_backup_identities,resulting_revisions,activity_ids,undo_group_id,outbox_ids,started_at,commit_decided_at,completed_at,error_digest}`.
Audit `status` is exactly `applied`, `undone`, `rejected`, `conflicted`, `expired`, or `indeterminate`; `validation_status` is `pass`, `fail`, or `not_run`.
`actor` is the unauthenticated apply/undo label retained for audit; `endpoint_scope` and key digest identify replay scope without retaining the key.
`decision_record_ids` is the sorted exact accepted/rejected decision-row id set used by apply and empty for undo; `decision_rows_digest` hashes the canonical complete rows in that order.
`accepted_op_ids` is the same exact effective dependency-closed accepted set in
intent and WAL; it is empty for undo. Decision rows remain evidence of explicit
choices but cannot add a blocked, conflicted, invalid, rejected, or undecided
operation to staging, intent, WAL, audit, or publication.
`target_backup_identities` is the canonical concept-id map to `{relative_name,device,inode,mode,size,digest}`.
`target_before_identities` and `target_resulting_identities` are canonical
concept-id maps to the complete §3 identity object. The before map may use the
literal `absent` only for a proved create target; every successful terminal
resulting map is concrete. Rejected/conflicted pre-publication audit repeats the
verified before map and has an empty resulting map. Indeterminate audit preserves
every identity actually verified and never fills an unknown entry from a digest.
`parent_transaction_id` is null for apply and the applied transaction id for undo.
Maps are canonical concept-id maps; fields inapplicable to undo are empty arrays or null exactly as declared by operation-specific validation, never omitted.
The complete audit payload, including actor label, endpoint/idempotency scope and key digest, transaction-intent digest, decision ids/digest, validation identity/result, history digest/backups, resulting revisions, activity ids, undo group, timestamps, and transaction linkage, is retained unchanged for at least 180 days after latest lineage activity.

The canonical repository-owned history manifest path is `transactions/<transaction_id>/history.json`.
Its immutable closed payload is exactly `{schema:1,transaction_id,operation,parent_transaction_id,proposal_id,proposal_revision,undo_group_id,targets:[{concept,before_digest,after_digest,before_identity,staged_identity,backup_identity}],created_at,expires_at,checksum}`. It has no `state` or `resulting_identity` field and is never replaced to express transaction progress. It is prepared authority only: WAL owns transaction phase and per-target publication progress/resulting identity, while terminal transaction audit owns the durable lifecycle outcome after WAL retirement.
Targets use canonical concept order and each `backup_identity` has exactly `{relative_name,device,inode,mode,size,digest}`.
`history_manifest_digest` is the digest of the one immutable canonical manifest and the transaction audit MUST repeat that digest and the exact target backup identity map.
The intent's `history.manifest_digest` and every audit's
`history_manifest_digest` MUST equal those same immutable bytes; their target
identities and digests MUST equal the intent and WAL; and the WAL's immutable
`transaction_intent_digest` MUST identify that exact intent. This
intent → immutable-history → WAL association is validated before `prepared` and
again at every recovery/finalization step, before undo, and before WAL retirement.

The durable endpoint result is exactly `{schema:1,record_id,scope,key_digest,request_fingerprint,state,http_status,response_digest,response,transaction_id,created_at,expires_at,checksum}` where `record_id` is the server-assigned `rec_` identity referenced by the domain row, service intent/transaction, outbox receipt, audit, and any compacting tombstone, and state is `prepared`, `terminal`, or `indeterminate`.
Prepared has null status/digest/response; terminal has exact success or error HTTP status/envelope; indeterminate has HTTP `409`, the exact `indeterminate_outcome` envelope and matching digest.
A tombstone is exactly `{schema:1,result_record_id,scope,key_digest,request_fingerprint,outcome,response_digest,transaction_id,created_at,expires_at,checksum}` where `result_record_id` preserves the compacted endpoint-result identity and outcome is `terminal_compacted` or `indeterminate`.
An outbox receipt is exactly `{schema:1,outbox_id,aggregate_kind,aggregate_id,proposal_id,proposal_revision,event_type,mutation_intent_id,result_record_id,payload,payload_digest,state,event_cursor,attempts,created_at,updated_at,checksum}` where `payload` is the exact closed canonical proposal notification object, `payload_digest` hashes that object, `mutation_intent_id` is the owning `svc_` service intent (or the apply/undo transaction id for transaction notifications), and `result_record_id` is the exact endpoint-result record linked by that intent/transaction. `aggregate_id`, `proposal_id`, `proposal_revision`, event payload identity, mutation identity, and result identity MUST agree. State is `pending` or `appended`; event cursor is null until appended.
The reconciler validates the receipt and delivers its stored immutable `payload`; it MUST NOT call a read-model projector, resolve a recipe, or reconstruct notification bytes from current mutable state. Event append is idempotent by `outbox_id` and delivery is at least once: recovery may observe or append the stored notification, clients deduplicate by global cursor and `outbox_id`, and then refetch canonical state.
No exactly-once delivery claim is made.

A corruption marker is exactly `{schema:1,corruption_id,repository_id,authority_kind,authority_identity,reason,observed_digest,diagnostic_copy,detected_at,repaired_at,repair_record_id,checksum}`.
`authority_kind` is exactly `repository`, `aggregate`, `checkpoint`, `preview`, `endpoint_result`, or `transaction_file`, and MUST equal `authority_identity.kind`.
`authority_identity` is the following closed discriminated union:

| `kind` | Exact identity object |
|---|---|
  | `repository` | `{kind:"repository",repository_id}` |
| `aggregate` | `{kind:"aggregate",aggregate,generation,sequence,relative_path}` where `aggregate` is `proposals`, `decisions`, `transactions`, `endpoint_results`, `outbox`, `tombstones`, or `corruption`; both null means the active log as a whole, non-null generation plus null sequence means that generation as a whole, and non-null generation plus non-null sequence means one row; sequence non-null with generation null is invalid |
| `checkpoint` | `{kind:"checkpoint",aggregate,generation,relative_path}` using the same aggregate enum and a non-negative safe generation |
| `preview` | `{kind:"preview",preview_digest,relative_path}` |
| `endpoint_result` | `{kind:"endpoint_result",scope,key_digest,sequence,relative_path}` where scope is the exact route template, key digest is a Digest, and sequence is a non-negative safe integer |
  | `transaction_file` | `{kind:"transaction_file",transaction_id,component,relative_path}` where `transaction_id` is the closed apply/undo transaction-id union from §3 and component is `intent`, `wal`, `history`, `stage_leaf`, or `backup_leaf` |

Every identity string uses its canonical primitive; every `relative_path` uses the repository-relative-path primitive; no union member accepts additional or null fields except the aggregate null pair stated above.
`reason` is exactly `missing`, `malformed`, `unsupported_schema`, `checksum_mismatch`, `digest_mismatch`, `sequence_gap`, `duplicate_identity`, `type_mismatch`, `owner_mismatch`, `mode_mismatch`, `link_count_mismatch`, `path_substitution`, or `authority_contradiction`.
`observed_digest` is a Digest when any bytes were read and is null only for `missing`, `type_mismatch`, `owner_mismatch`, `mode_mismatch`, `link_count_mismatch`, or `path_substitution` before readable regular bytes were obtained.
`diagnostic_copy` is either null or exactly `{relative_path,digest,size}` using the primitives above and is never authority.
`detected_at` is non-null; `repaired_at` and `repair_record_id` are either both null on an unrepaired marker or both non-null on its repaired successor.
The marker is durable authority that blocks the affected aggregate until explicit operator repair appends a closed repair record and a successor corruption marker setting `repaired_at`/`repair_record_id`.
Quarantine is a best-effort non-authoritative copy only; failure to copy does not remove the marker, and omission of corrupt authority is never recovery.

A repair payload is exactly `{schema:1,repair_record_id,corruption_id,repository_id,action,replacement_authority,terminal_classification,operator_label,reason,created_at,checksum}`.
`repair_record_id` and `corruption_id` are `rec_` and `cor_` ids respectively; `repository_id` must match the marker; `action` is `replace_authority` or `classify_indeterminate`.
For replacement, `replacement_authority` is required as `{relative_path,digest,size,source_provenance}` and `terminal_classification` is null.
Its path, digest, and size use the primitives above; `source_provenance` is exactly `{kind,source_digest,operator_note}` where kind is `validated_backup` or `operator_supplied`, source digest is a non-null Digest of the supplied source bytes, and operator note is 1–1,024 code points.
For classification, `replacement_authority` is null and `terminal_classification` is exactly `{scope,key_digest,transaction_id,outcome:"indeterminate"}`; scope is the exact endpoint route template, key digest is a Digest, transaction id is a canonical transaction id, and no field is nullable.
`operator_label` is an unauthenticated local label; reason is 1–1,024 code points.
Repair protocol is exact: stop serving proposal routes; hold session lease and repository append/rotation lock; verify the original marker is current; stage and fully validate the replacement or classification; atomically publish and parent-fsync replacement authority if any; append/fsync the repair row; append/fsync the successor marker referencing it; regenerate and verify the corruption checkpoint; rerun full repository recovery; only then resume routes.
The successor marker reuses the same `corruption_id`, repository/authority identity, reason, observed digest, diagnostic copy, and `detected_at`; it changes only `repaired_at`, `repair_record_id`, wrapper sequence, and checksum, and latest valid marker row wins.
Failure at any step preserves the original blocking marker and cannot make quarantine authoritative.

Audit projection is exactly `{schema:1,proposal_id,proposal_revision,proposal_record_id,decision_record_ids,withdrawal_record_id,transaction_ids,endpoint_result_digests,validation_context_digest,validation_digest,resulting_revisions,activity_ids,outbox_ids,undo_group_id,undo_transaction_ids,retention}` with nullable singular ids only when the event never occurred.

The canonical proposal read model has exactly `schema`, `proposal`, `state`, `operation_states`, `latest_decisions`, `decision_set_token`, `conflicts`, `transaction_attempts`, `read_model_token`, `apply_result`, `undo_result`, and `retention`.
`proposal` is the immutable revision and verified `preview` transport projection while preview bytes remain retained; after byte expiry `preview` is `{schema:1,expired:true,expired_at}` while `proposal.preview_ref` remains.
`operation_states` is sorted by op id and contains `{op_id,local_validity,explicit_decision,blockers,conflicts,effective_state}`.
`latest_decisions` is the sorted latest decision row array.
Apply and undo results are nullable.
`transaction_attempts` is an array of closed transaction-progress projections sorted by `(started_at,transaction_id)` descending.
Each projection is exactly `{schema:1,operation,transaction_id,parent_transaction_id,status,phase,commit_decided,completed_targets,total_targets,recovery_attempts,started_at,updated_at,retry_after_ms,error_code}`.
Its `status` enum is `preparing`, `rollback_pending`, `forward_committed`, `publishing`, `finalizing`, `recovering`, or `indeterminate`.
Projection precedence is: WAL `indeterminate` → `indeterminate`; otherwise `recovery_attempts>0` on any nonterminal phase → `recovering`; otherwise phase maps `prepared→preparing`, `rolling_back→rollback_pending`, `commit_decided→forward_committed`, `publishing→publishing`, and `content_published|finalizing→finalizing`.
Terminal `committed`/`rolled_back` rows are not progress projections and appear only in apply/undo result or audit.
`completed_targets` is the count of true publish bits for forward phases or true rollback bits for rollback; `total_targets` is target length; `retry_after_ms` is 500 for every nonterminal non-indeterminate projection and null for indeterminate; `error_code` is null except `indeterminate_outcome` for indeterminate or `transaction_recovering` while recovering.
At most one nonterminal attempt per operation may exist for a proposal revision; a contradiction is storage corruption.
`retention` is exactly `{audit_expires_at,preview_expires_at,replay_expires_at,undo_expires_at}` with nullable timestamps only when that artifact never existed.
The per-proposal read-model token hashes the exact proposal row checksum, complete latest decision rows, applicable withdrawal/transaction records, proposal-specific service-intent/result identities, preview digest, conflict/current-bundle snapshot inputs, `validation_context_digest`, exact expiry instant, config/capability sets, and all three runtime identities. It excludes aggregate high waters, outbox sequence, unrelated endpoint results, tombstones, corruption rows, and every other proposal. Aggregate high waters remain page-snapshot/cursor authority only.

### 5.8 Valid and invalid envelope examples

Valid create input:

```json
{
  "schema": 1,
  "mode": "proposal",
  "author": "agent-local",
  "summary": "Add the customer relation",
  "rationale": "Make the dependency explicit without changing direct mode.",
  "expected_snapshot": {
    "schema": 1,
    "captured_at": "2026-07-14T12:34:56.789Z",
    "concepts": [
      {"concept":"tables/customers","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
      {"concept":"tables/orders","revision":"abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"}
    ],
      "capabilities": ["okf.cap.typed_relations"],
      "config_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "validation_context_digest": "sha256:56789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234",
      "validator_id": "okf-validator@1.0+sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "serializer_id": "okf-roundtrip@1.0+sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "operation_runtime_id": "proposal-ops@1.0+sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "snapshot_digest": "sha256:3410b9d4bb94b97cf8fc454eecf119506a44d47986f15400210b8794da7213aa"
  },
  "operations": [{
    "schema": 1,
    "op_id": "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
    "kind": "link_add",
    "target": "tables/orders",
    "args": {"destination":"tables/customers","label":"customers","relation":"depends_on","detail":null,"allow_forward_reference":false},
    "reads": ["tables/customers","tables/orders"],
    "writes": ["tables/orders"],
    "expected_revisions": {
      "tables/customers":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "tables/orders":"abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    },
    "depends_on": [],
    "extensions": {}
  }],
  "provenance": [{"kind":"comment","value":"01J2Y8N5Z9"}],
  "extensions": {}
}
```

Invalid inputs include `{"schema":1,"schema":2}` (duplicate key), an unknown `command` operation, `target:"../secrets"`, undeclared destination reads, `allow_forward_reference:true`, a boolean offset, a dependency cycle, an unknown core field, an unsupported schema, or bytes beyond a cap.
Each is rejected before persistence, preview publication, or canonical mutation.

A valid reserved-file finding is `{"code":"log.bad_date_heading","severity":"warning","concept":null,"line":0,"message":"Log heading has no meaningful source line.","fingerprint":"sha256:…"}` when that rule is frozen as reserved-file-scoped. A valid concept-scoped finding uses a canonical id such as `"concept":"tables/orders"` and either a one-based source line or `0`. The same concept-scoped finding with `concept:null`, a null concept for any scope outside the four permitted non-concept scopes, `line:null`, or a negative line is invalid. In a mixed array the null-concept finding precedes every concept finding under the §5.3 sort.

Every other schema family has a compact valid and invalid conformance example below; ellipses are explanatory notation, not valid JSON.

| Family | Valid example | Invalid example and required rejection |
|---|---|---|
| Proposal/withdrawal | `{"schema":1,"proposal_id":"01J2Y8N5Z9Q3A7K6M4T1V0WXYZ","proposal_revision":1,"preview_ref":{"schema":1,"preview_digest":"sha256:…","artifact_bytes":4096},…}`; withdrawal carries exact revision/token | inline `preview` in persisted proposal or second withdrawal → `invalid_record` |
| Repository root | schema 1 with exact aggregate map, valid 32-byte cursor key ring, active key, limits digest and checksum | unknown aggregate, expired active key, duplicate key id, bad checksum → startup unavailable |
| Snapshot/preview | snapshot and preview carry the same `validation_context_digest` and all three runtime ids; preview digest matches the sole artifact | missing context authority/artifact or digest/length/runtime mismatch → `preview_authority_unavailable` |
| Operation | caller `op_id`, exact discriminant args/read/write/dependency sets | server-generated op id, reused changed op id, unknown arg → `invalid_request` |
| Decision request/success | request `{"schema":1,"decision":"accepted","reason":"","previous_row_token":null,"read_model_token":"sha256:…"}`; success `{"schema":1,"data":{"decision":{…},"decision_set_token":"sha256:…","read_model_token":"sha256:…"}}` | stale row token or missing reason for prerequisite rejection → `decision_conflict` / `invalid_request` |
| Conflict | `{"schema":1,"code":"snapshot_changed","proposal_id":"…","proposal_revision":1,"op_id":null,"concept":null,"expected":"sha256:…","actual":"sha256:…","message":"Snapshot changed.","detected_at":"2026-07-14T12:00:00.000Z"}` | unknown code or null op on op-scoped conflict → `invalid_projection` |
| Transaction finalization intent | exact accepted authorities, candidate ids/payloads or closed templates, audit checksum placeholders in both branch paths, allowed placeholder set, history/target linkage, retention and checksum; intent hashes retain candidate-checksum placeholders | missing candidate, concrete audit checksum in the intent digest, cyclic/downstream-first substitution, mutable-state recipe, unknown/embedded placeholder, id/digest/link mismatch → fail closed before decision or corruption+indeterminate after decision |
| WAL | one target: `commit_decided`/`[false]` may become `content_published`/`[true]` only after descriptor-relative exchange/rename, exact source/destination/resulting-identity verification, parent fsync, and resulting-identity capture; two targets: `commit_decided`/`[false,false]` must become `publishing`/`[true,false]` before `content_published`/`[true,true]`; every record retains the exact immutable `transaction_intent_digest` | missing/changed intent or identity, same-byte/different-inode, one-target `publishing`, multi-target `commit_decided → content_published`, `publishing` with false commit flag, or any non-proper-prefix publishing bitmap → `storage_corrupt` |
| Endpoint result | terminal record contains exact HTTP status, response and matching digest | terminal with null response or mismatched scope/fingerprint → corruption marker |
| Transaction audit | apply row links proposal, decisions, preview, validation, revisions, activities, group and outbox | missing any applicable immutable link → `storage_corrupt` |
| History manifest | canonical transaction path, sorted targets, exact backup identities, matching audit digest | path/digest/backup mismatch → undo unavailable and `storage_corrupt` |
| Tombstone | `terminal_compacted` retains scope/key/fingerprint/response digest/transaction | tombstone lacking fingerprint or uncertain tombstone marked terminal → corruption marker |
| Outbox receipt | pending receipt stores exact payload and intent/result/revision bindings with null cursor; appended receipt adds cursor | reconstructed/missing payload, binding mismatch, appended with null cursor, or changed payload/digest → corruption marker |
| Checkpoint/log wrapper | contiguous sequence/generation and matching record/checksum digests | gap, duplicate, unknown kind, checksum mismatch → aggregate unavailable |
| Corruption/repair | marker identifies exact authority and optional diagnostic copy; repair names marker | quarantine copy without marker or marker silently omitted → service remains unavailable |
| Repair row | closed replacement or indeterminate classification followed by successor marker/checkpoint | mixed action fields, stale marker, or unverified replacement → original marker remains blocking |
| Read model/page | one verified preview and token over every authority input, including validation context | embedded preview mismatch or page/token omits validation-context, expiry, or runtime input → fail closed |
| Transaction progress | nonterminal WAL maps to one closed `transaction_attempts` row by precedence | terminal WAL shown as progress, ad hoc boolean, or two active same-operation attempts → `storage_corrupt` |
| Legacy projection/regenerate | `{"legacy_identity":{"legacy_id":"legacy-7","source_generation":3},…}` and regenerate request repeats both fields | bare legacy id or changed generation → `legacy_identity_conflict` |
| Typed error | `{"schema":1,"error":{"code":"repository_quota_exceeded","message":"Proposal repository quota is exhausted.","request_id":"req_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ","retryable":false,"resync_required":false,"details":[{"kind":"quota","resource":"repository_bytes","limit":2147483648,"actual":2147483649}]}}` | unbounded/free-form detail or proposal bytes in detail → `internal_contract_error` |

Error `details` is an array of at most 32 closed discriminated objects and at most 16 KiB canonical bytes total.
Allowed kinds are `field` (`path`,`rule`), `conflict` (`op_id`,`concept`,`expected`,`actual`), `quota` (`resource`,`limit`,`actual`), `dependency` (`op_id`,`path`), and `storage` (`authority_kind`,`corruption_id`); no other keys are allowed.

### 5.9 Schema proof matrix

| Schema surface | Valid proof | Invalid proof | Required layer |
|---|---|---|---|
| Proposal revision and supersession | Canonical round trip and immutable digest | missing/unknown field, revision gap, wrong predecessor | `SCHEMA-01` contract + persistence |
| Every operation union member | Shared valid fixture stages deterministically | wrong discriminant/args/read-write set/expected revision | `SCHEMA-02` property + handler |
| Snapshot/preview/diff/validation | Byte and digest fixture parity, nullable finding ownership, bundle-first sort, and shared validation context | wrong finding nullability/order, context, digest, base64, profile, or truncation | `SCHEMA-03` Python/CLI/browser fixture |
| Decision/token | First and replacement CAS | bad token, unsupported decision, missing required reject reason | `SCHEMA-04` cross-process |
| Conflict and validation-finding projection | Every allowed conflict code, nullable finding ownership, `line:0`, and null-safe sort order | unknown conflict code, impossible null ownership, noncanonical concept, or negative line | `SCHEMA-05` projector plus `SCHEMA-03` fixture |
| Apply/intent/WAL/result/outbox | Serialize both operation-specific intents, exact candidates/templates, both audit checksum placeholder paths, every phase, immutable intent digest over placeholders, deterministic audit-first substitution, and terminal result | concrete candidate checksum in the intent digest, illegal placeholder/link/phase/result combination, cyclic substitution, or checksum | `SCHEMA-06` fault injection |
| Service mutation WAL/intent | Serialize every operation/phase with exact row, notification, result, preview and accepted-token bindings | skipped phase, changed duplicate, missing binding, prepared endpoint without intent | `SERVICE-01..07` fault/restart |
| Undo/withdrawal/audit links | Complete trace round trip | missing link, stale authority, partial restore result | `SCHEMA-07` E2E |
| Checkpoint/log wrapper | Merge and restart from every generation | sequence/checksum/kind mismatch | `SCHEMA-08` storage recovery |
| Notification/read model/page | Canonical token-qualified reads | unknown event, malformed item, skewed token | `SCHEMA-09` HTTP/browser |
| Legacy projection | Bounded valid summary | malformed row never becomes applicable | `SCHEMA-10` migration |

Every public object also runs the same duplicate-key, unknown-field, wrong-type, boolean-as-integer, over-limit, invalid UTF-8, and unsupported-version corpus as `SCHEMA-11`.

## 6. Deterministic snapshot capture and staging

**Foundation Bundle 2 is implemented:** production bundle snapshot capture, frozen validation, the identified round-trip document codec, and pure capture → stage → validate → preview composition are available as library adapters. Those adapters alone do not activate transport; Bundle 3 now composes them into the experimental seven-route HTTP/CLI surface, and its transport-neutral apply/undo transaction core is implemented experimentally. The Studio UI remains unavailable. Capture acquires shared ownership of the bundle transaction lock, pins the bundle root by a trusted directory descriptor, and scans using current §5 visibility rules. Shared acquisition retries only within one monotonic 15-second bound and emits typed retryable `bundle_busy` on timeout; the active HTTP adapter maps that result to `503 bundle_busy`. There is no unlocked fallback. Ordinary current readers do not claim shared ownership.
Current integrated okf-loom source/config writers acquire exclusive ownership of that same lock before changing source or scan-control bytes and hold it through their existing atomic publication envelope. Direct-mode writers normally wait without a transaction-lock timeout and retain their existing atomic-but-not-generally-fsynced durability semantics. Experimental Proposal Review apply/undo publication performs the explicit file and parent-directory fsync protocol in §8. Shared capture and exclusive writers obey the global ordering in §8. Direct editors that do not participate in the lock remain supported only through the exact double-scan check below; the protocol does not mistake them for coordinated writers.

The bundle-lock async lifecycle contract is explicit: capture/shared acquisition has one 15-second deadline and returns retryable `bundle_busy` with no capture state; direct writers wait normally; cancellation or timeout removes the waiter and closes its descriptor so no late acquisition can occur; forked children close inherited owner/turnstile descriptors; and process death releases kernel ownership. A retry starts a new acquisition and re-reads authority rather than resuming stale local state.
Capture records the complete concept inventory plus the reserved-file bytes, visible non-concept inventory, scan-control bytes/manifests, load warnings, and frozen validation policy that form `validation_context_digest`. It reads every visible source or control leaf descriptor-relatively without following symlinks. A visible symlink in any concept, reserved source, non-concept source needed by validation, configuration, ignore/include control, or scan manifest fails capture closed, even where ordinary permissive `Bundle.load` behavior would tolerate it.
It revalidates device/inode/type and containment after open, reads exact bytes once, parses only from those bytes, and computes revisions. Its iterative descriptor walker bounds every encountered entry before sorting, per-directory and total entries, depth, component bytes, and relative-path bytes. It then performs an exact second inventory-and-identity scan under the pinned root and compares concept, reserved-file, validation-relevant non-concept, scan-control, config, capability, warning, and policy inputs. Any addition, removal, identity/size/byte change, symlink substitution, or authority change causes one full retry; a second mismatch emits typed `snapshot_unstable`. `409 snapshot_unstable` is reserved for a future HTTP adapter. No instability path silently accepts the first scan.

### 6.1 ASYNC_LIFECYCLE_MATRIX — Foundation Bundle 2

| Scenario | Deadline/abort owner | Mutation phase | Typed result | Persisted state | Late-result behavior | Retry behavior | Cleanup/ownership transfer | Exact proof |
|---|---|---|---|---|---|---|---|---|
| Shared lock timeout | Capture monotonic deadline | None | retryable `bundle_busy` | None | No late acquisition | New capture rereads authority | waiter removed; root/lock fds closed | `tests/test_proposal_snapshot_adapter.py::test_shared_capture_timeout_is_retryable_bundle_busy` |
| First scan unstable, retry budget exhausted | Same original deadline | None | retryable `bundle_busy` before second acquisition | None | Expired acquisition cannot complete | Caller starts a new deadline | both attempts release shared ownership and descriptors | `tests/test_proposal_snapshot_adapter.py::test_instability_retry_uses_one_lock_deadline_and_leaks_no_fds` |
| One direct-editor race then stable | Capture retry owner | None | immutable captured snapshot | None | Later writes cannot alter accepted facts | Exactly one full retry from scratch | shared lock released before staging; immutable facts transfer to pure owner | `tests/test_proposal_snapshot_adapter.py::test_unlocked_edit_between_scans_retries_from_scratch` |
| Nested repository marker replacement | Capture retry owner | None | stable snapshot or retryable `snapshot_unstable` | None | Prior marker identity is never accepted | Full retry compares canonical marker identity | all probe/root fds closed | `tests/test_proposal_snapshot_adapter.py::test_nested_git_marker_identity_is_bound_and_replacement_retries` |
| Exception/unsafe path | Capture stack owner | None | `snapshot_unsafe` or `snapshot_limit_exceeded` | None | No partial facts escape | Corrected bundle requires a new capture | iterator and every descriptor close in `finally` | `tests/test_proposal_snapshot_adapter.py::test_unsafe_backslash_name_and_excessive_depth_fail_typed` |
| Frozen validation after release | Immutable-facts owner | Pure in-memory stage only | deterministic report/preview | None in Foundation Bundle 2 | Live root/config/assets cannot affect output | Recapture required for new authority | no filesystem ownership remains | `tests/test_proposal_frozen_validator.py::test_frozen_validation_survives_deleted_root_and_performs_no_path_io` |

The server verifies caller-provided expected revisions against the captured map.
It canonicalizes paths, validates declared read/write sets, rejects undeclared handler access, and orders operations by Kahn topological sort with ascending bytewise `op_id` among ready nodes.
For apply, the server first derives the exact effective accepted set from §7.3,
proves that set non-empty and dependency-closed, and uses that one sorted set as
the sole `accepted_op_ids` authority for restaging and every later intent, WAL,
audit, and publication decision. An explicitly accepted operation excluded by
derived validity, conflict, or blocker state is not staged. Staging begins from
one immutable in-memory snapshot and executes only that effective set in its
canonical topological order.
Each handler receives only declared read bytes and a private staged write map; it MUST NOT access disk, clock, random, network, process environment, plugins, or active code.
An operation reads the latest staged bytes produced by its ordered prerequisites, otherwise snapshot bytes.
The final before bytes are snapshot bytes; after bytes are the final staged map.

Preview creation serializes staged concepts through the same identified production round-trip writer that apply will use.
Before/after bytes and their digests exist only in the immutable content-addressed preview artifact referenced by the revision.
Before proposal persistence, the server canonical-serializes the complete projected read model, including base64 expansion, and rejects `read_model_too_large` if it exceeds 40 MiB; it MUST NOT persist a proposal that cannot be read under the response cap.
Create/supersede preview bytes are first written beneath the repository's private `preview-staging/<intent_id>/` directory, fsynced, verified, and named in the service intent before that intent reaches `prepared`. After `prepared`, recovery promotes those exact bytes to the content-addressed preview path by no-replace publication or verifies an identical existing artifact, then appends the exact proposal row. The proposal row, staged reference, promoted reference, and preview digest/length MUST agree. No unprepared staged file can authorize a proposal, and no promoted preview lacking an intent/domain reference can authorize mutation.
Startup recovery scans preview staging before serving. It preserves every file referenced by a coherent nonterminal or indeterminate service intent, resumes promotion for a recoverable intent, and removes only identity-checked files that are unreferenced and either expired or belong to an abandoned operation with no endpoint/service-intent authority. Content-addressed previews remain while referenced by retained proposal, service-intent, audit, or corruption authority; unreferenced promoted artifacts are removed only after the same 30-day preview horizon and a verified repository snapshot proves no reference. Cleanup failure retains bytes and fails closed when quota or authority cannot be established.
Apply MUST load and verify that artifact, independently restage using its before bytes and the exact stored operations, then require byte-for-byte equality with every stored `after_base64`, diff digest, preview digest, and validation digest.
Any difference is `500 contract_parity_failure` and writes no content.

Validation runs over the complete staged bundle snapshot, not each operation in isolation. The Proposal Review validator receives only the pinned capture: concept and staged bytes, captured reserved bytes, captured validation-relevant non-concept inventory, captured scan-control bytes/manifests, captured load warnings, and the frozen validation policy. It MUST NOT reopen the bundle, consult the live filesystem, resolve a path outside the pinned bundle, or perform host lookup. A lexical path escape is reported deterministically from captured text as a frozen-policy finding; neither existence checks nor canonicalization may touch the escaped host path.
Preview is fresh only while current snapshot digest, `validation_context_digest`, capability set, config digest, proposal revision, preview digest, `validator_id`, `serializer_id`, and `operation_runtime_id` match, and `expires_at` has not passed.
There is no preview-refresh mutation: refresh creates a superseding immutable revision.
There is no automatic, semantic, textual, or three-way rebase.

## 7. Ownership and state machines

### 7.1 State ownership

| State | Authority | Writer | Derived consumers | Proof |
|---|---|---|---|---|
| Concept bytes/revision | Bundle Markdown | Direct funnel or committed transaction | Snapshot/conflict/read model | `STATE-01` unit + cross-process |
| Proposal revision | Immutable proposal log + checkpoint | Proposal service only | Preview/read model/UI | `STATE-02` persistence restart |
| Explicit decision | Latest valid decision row | Decision endpoint under revision lock | Decision set | `STATE-03` CAS race |
| Blocked/conflicted/invalid | Pure projector | No persisted writer | UI/apply preflight | `STATE-04` table fixtures |
| Proposal lifecycle | Immutable records plus derived facts | Withdraw/apply/supersede records | List/read/UI | `STATE-05` transition model |
| Apply phase and target identity | WAL/result record plus intent-bound before/staged/resulting identity chain | Apply coordinator/recovery | Read model/replay | `STATE-06` fault injection |
| Event cursor | Existing global event store | Event appender | SSE/polling | `STATE-07` cross-process |
| Browser route/generation | Current document | Browser controller | DOM/cache/apply enablement | `STATE-08` browser race |
| Undo availability | Immutable history manifest + terminal transaction audit/WAL authority + retention + exact current equality to the parent apply resulting identity | Apply/undo coordinator | Proposal audit projection | `STATE-09` expiry/ABA faults |
| Idempotency result | Endpoint-scoped result record, then tombstone | Endpoint coordinator/recovery | Replay only | `STATE-10` crash/compaction |
| Service mutation publication | Closed `service-wal/<intent_id>.json` plus mutually verified domain/outbox/result rows | Repository service coordinator/recovery | Create/supersede/decision/withdraw replay and audit | `SERVICE-01..07` |
| Transaction audit | Transaction aggregate/checkpoint bound to intent, immutable history, and before/resulting identities | Coordinator/recovery | Audit/read model | `STATE-11` reconciliation |
| Pagination snapshot | Signed stateless immutable cursor payload | Read repository | Page walker; no retention pin | `STATE-12` concurrency |
| Cache freshness | Full cache key and canonical tokens | Browser generation owner | Render controls | `STATE-13` stale response |
| Expiry | Persisted exact expiry instant + server UTC clock | Pure projector | State/read model/apply preflight | `STATE-14` boundary clock |
| Runtime and validation identities | Persisted preview/snapshot runtime ids plus `validation_context_digest` | Build/runtime provider and pinned capture | Freshness/apply | `STATE-15` version/context drift |
| Apply-enable tuple | `(id,rev,read_model_token,decision_set_token,preview_digest,snapshot_digest,validation_context_digest,runtime ids)` | Browser current read | Apply control | `STATE-16` invalidation |
| Outbox delivery | Outbox receipt's exact stored payload plus event-store observation | Outbox reconciler | Notification only; no mutable-state reconstruction | `STATE-17`, `OUTBOX-01..04` |
| Transaction waiters | One in-memory waiter set keyed by transaction id; non-authoritative | Request lifecycle | HTTP/browser pending UI | `STATE-18` supersession/restart |
| Transaction progress projection | Verified nonterminal WAL mapped by closed precedence | Pure read projector | HTTP/Studio status only | `STATE-19` projection algebra |

### 7.2 Proposal lifecycle

`draft` exists only inside an unsubmitted client and is never persisted.
The closed persisted/read-model state enum is `in_review`, `ready`, `conflicted`, `applied`, `withdrawn`, `superseded`, `expired`, or `audit_expired`.
Terminal revision states are `applied`, `withdrawn`, `superseded`, `expired`, and `audit_expired`.
Persisted derived states use the first matching row in this precedence order: `audit_expired`, `applied`, `withdrawn`, `superseded`, `expired`, `conflicted`, `ready`, `in_review`.

| From | Event/guard | To | Side effect | Proof |
|---|---|---|---|---|
| none | Valid creation and durable preview | `in_review` | Append immutable revision and notification | `LIFE-01` HTTP/restart |
| `in_review` | Every op accepted or rejected; accepted set non-empty, dependency-closed, valid, fresh | `ready` | Derived only | `LIFE-02` projector |
| `in_review`/`ready` | Any conflict | `conflicted` | Derived only; decisions unchanged | `LIFE-03` mutation race |
| `conflicted` | Canonical bytes restored exactly before expiry | prior derived review state | Derived only | `LIFE-04` restoration |
| `in_review`/`ready`/`conflicted` | New revision names current revision | `superseded` | Append new immutable revision; no decision transfer | `LIFE-05` supersession |
| `in_review`/`ready`/`conflicted` | User withdraw with matching read token | `withdrawn` | Append withdrawal record | `LIFE-06` CAS |
| `ready` | Apply commits | `applied` | Durable result/activity/outbox | `LIFE-07` E2E |
| any unapplied and not withdrawn/superseded | Preview bytes pass `expires_at` | `expired` | One outbox identity; at-least-once notification delivery; audit retained | `LIFE-08` clock boundary |
| terminal | Any prohibited transition | unchanged | `409 invalid_state` | `LIFE-09` exhaustive table |
| any retained terminal | Audit horizon passes and checkpoint pruning succeeds | `audit_expired` tombstone projection | Preserve non-reusable lineage/transaction identities | `LIFE-10` retention |

An older revision is `superseded` even if its successor later expires or is withdrawn.
Withdrawn, expired, applied, superseded, and audit-expired revisions cannot be superseded, withdrawn, decided, applied, or undone except that only an applied revision may enter the separately governed undo transaction.

### 7.3 Per-operation state, dependencies, and supersession

The three inputs are independent: `local_validity` is `valid` or `invalid`; `explicit_decision` is `undecided`, `accepted`, or `rejected`; derived `blockers` and `conflicts` are sorted arrays.
The closed `effective_state` enum and precedence are: `invalid` if local validity is invalid; otherwise `conflicted` if conflicts are non-empty; otherwise `rejected` if explicitly rejected; otherwise `blocked` if blockers are non-empty; otherwise `accepted` if explicitly accepted; otherwise `undecided`.

| Local validity | Conflict | Decision | Blocker | Effective state | Apply membership | Proof |
|---|---|---|---|---|---|---|
| invalid | any | any | any | `invalid` | no | `DEP-01` |
| valid | present | any | any | `conflicted` | no | `DEP-02` |
| valid | none | rejected | any | `rejected` | no | `DEP-03` |
| valid | none | accepted/undecided | present | `blocked` | no | `DEP-04` |
| valid | none | accepted | none | `accepted` | yes | `DEP-05` |
| valid | none | undecided | none | `undecided` | no | `DEP-06` |

Every blocking path is returned as the lexicographically sorted shortest `op_id` path from dependent to root blocker.
The server never changes an explicit decision while deriving state.
The apply set is exactly the sorted operations whose `effective_state` is
`accepted`, and it MUST be non-empty and dependency-closed. That set—not the
larger set of explicit `accepted` decisions—is the sole value serialized as
`accepted_op_ids` and the sole operation set permitted to restage, validate,
write intent/WAL/audit, or affect target bytes.
Supersession transfers no decision.
The UI MAY offer a one-action re-review suggestion only when canonical operation JSON, expected input bytes, after bytes, and complete upstream dependency closure are byte-identical; activating it still appends new decision rows for the new revision.

Supersede capture and preview preparation are speculative until final publication. While holding the final repository append/rotation and proposal-lineage locks, the service MUST re-read the lineage, prove that `supersedes_revision` is still the unique current nonterminal revision, prove that no successor row or coherent successor service intent already exists, enforce the lineage/revision limits, and assign exactly `proposal_revision = supersedes_revision + 1`. The uniqueness key is `(proposal_id,supersedes_revision)` for successor intent/domain authority. Concurrent different keys may prepare work, but exactly one successor may reach a coherent prepared intent and domain append; every loser reconciles its own key to a terminal conflict/indeterminate result and MUST NOT append a second successor or reuse its preview as authority.

### 7.4 Conflict and apply/recovery states

| Condition | Result | Mutation | Proof |
|---|---|---|---|
| Full current snapshot digest differs | `snapshot_changed` plus affected concept detail where applicable | none | `CONFLICT-01` |
| Accepted op read differs | `read_revision_changed` | none | `CONFLICT-02` |
| Accepted op write differs | `write_revision_changed` | none | `CONFLICT-03` |
| Target missing/type/complete identity, capability/config, or validation-context authority differs | typed corresponding `target_missing`, `target_type_changed`, `target_identity_changed`, `capability_changed`, `config_changed`, or `validation_stale` conflict | none | `CONFLICT-04` |
| Decision/read-model token stale | `decision_stale` | none | `CONFLICT-05` |
| All authorities current | preflight proceeds | none before WAL | `CONFLICT-06` |

| Apply phase | Durable authority | Allowed next phase | Client-visible meaning | Proof |
|---|---|---|---|---|
| none | no WAL; no publish authority; an intent/history pair may be durably orphaned between ordered prepare steps | `prepared` only after the pair verifies, otherwise safe cleanup/fail closed | no canonical content transaction exists | `WAL-01` |
| `prepared` | verified intent + history + staged files/backups + fsynced WAL, commit false | `commit_decided` or `rolling_back` | no canonical rename permitted | `WAL-02` |
| `commit_decided` | fsynced forward decision, empty bitmap | exactly one target: `content_published` only after descriptor-relative exchange/rename, immediate and repeated exact source/destination/resulting-identity verification, parent fsync, and durable `[true]` WAL replacement; two or more targets: `publishing` only after the first such verified publication | retry/recovery must roll forward | `WAL-03` |
| `publishing` | two or more targets; forward decision + non-empty proper-prefix bitmap and exact resulting identities for that prefix | `publishing` for another proper prefix, or `content_published` after final verified exchange/rename, parent fsync, exact resulting-identity capture, and durable all-true WAL replacement | outcome pending; roll forward | `WAL-04` |
| `content_published` | all canonical bytes published and verified | `finalizing` | publication complete | `WAL-05` |
| `finalizing` | publication complete; intent/immutable history verified; exact candidate audit/activity/result/outbox prefix may be incomplete | `committed` | finish only exact intent metadata idempotently | `WAL-06` |
| `committed` | result + intent/history/audit/activity/outbox linked | terminal | replay endpoint result | `WAL-07` |
| `rolling_back`/`rolled_back` | no commit decision; reverse bitmap | rollback/terminal | no content effect when complete | `WAL-08` |
| corrupt/uncertain authority | durable corruption marker/tombstone | `indeterminate` | no automatic retry | `WAL-09` |

### 7.5 Retention, expiry, and undo states

| Artifact | Minimum horizon while session root is retained | Expiry behavior | Proof |
|---|---:|---|---|
| Proposal metadata, decisions, apply/undo audit | 180 days after latest lineage activity | Projection becomes `audit_expired`; ids may remain as tombstones | `RET-01` |
| Reviewed before/after bytes and diffs | 30 days after proposal creation, and never before apply terminal | Review becomes `expired`; cannot apply | `RET-02` |
| Endpoint replay result/authority, including service mutations and apply/undo | 7 days after terminal result, except keyless decision exact result at 24 hours | Certain result may compact; uncertain key becomes indeterminate tombstone through audit horizon; preview retention does not extend replay | `RET-03`, `RET-SVC-01..02` |
| Service mutation WAL intent | Certain committed intent/result linkage survives through the exact 180-day audit horizon and may prune only after that instant; indeterminate remains repair authority | Never age-delete nonterminal, referenced, or uncertain intent | `RET-SVC-02..03` |
| Transaction finalization intent and prepared WAL | Intent survives at least as long as its WAL, history, replay authority, undo authority, or audit linkage; nonterminal/uncertain intent and WAL remain until repaired | Never age-delete either half of a live or uncertain transaction association | `RET-04` |
| Undo snapshots/manifests | 30 days after commit | `expired`; audit remains | `RET-05` |
| SSE event generations | Current event-store contract | Gap requires canonical resync | `RET-06` |

Retention timestamps MUST be displayed in API and Studio.
Deletion of the configured session root by the operator ends these local guarantees; the product MUST say so.
Audit retention MUST NOT be described as retained diff bytes, replay authority, or undo availability.

Undo accepts only an applied transaction whose retained audit → intent → immutable history → backup chain verifies, whose terminal audit/WAL resulting identities agree, whose current canonical identity for every target exactly equals the parent apply's `target_resulting_identities` value, and whose `undo_group_id`, read-model token, and new idempotency key are exact. Revision or digest equality without device/inode/`ctime_ns`/mode/size/parent equality is a conflict: change-away/change-back is ABA and MUST restore nothing.
It locks and stages every restoration, validates the complete restored snapshot, preassigns an undo-specific finalization intent with exact parent transaction and nullable apply-only authority, and uses the same intent-before-history-before-WAL transaction protocol.
Its terminal outcomes are `undone`, `conflicted`, `expired`, and `indeterminate`.
No partial success exists.
Undo does not delete proposal, decisions, apply result, or audit links.
A compensating forward change requires a new reviewed proposal.

## 8. Atomic apply transaction and exact crash behavior

The complete global lock rank is: session-root lease → repository/proposal-store append/rotation lock → immutable read-snapshot lease → bundle transaction lock (shared for capture/read validation, exclusive for known writers/apply/undo/recovery) → sorted concept locks by canonical UTF-8 concept id → proposal-lineage lock → endpoint/idempotency-key lock → transaction lock → transaction intent/history lock → activity/event/outbox append lock.
No code may acquire an earlier lock while holding a later lock.
Proposal Review repository/transaction acquisitions are cross-process and bounded to 15 seconds. Proposal capture/shared or future proposal transaction bundle-lock timeout returns retryable `503 bundle_busy`; any other Proposal Review lock timeout returns `503 lock_unavailable`. Current direct-mode writers normally block for exclusive bundle ownership rather than adopting that proposal deadline. No bounded path permits mutation or a silent unlocked fallback.
These are proposal-repository locks layered around current behavior; they MUST NOT alter, alias, reorder internally, or weaken existing `path_locks` semantics used by direct mode.
Known bundle writers take exclusive bundle ownership before sorted concept locks; capture takes shared bundle ownership and never upgrades shared to exclusive. A writer waits for existing shared owners, and a new capture MUST NOT bypass an already-waiting exclusive writer. The implementation MUST use a starvation-safe cross-process primitive or equivalent queued protocol and prove the ordering; lock-file existence alone is not ownership.
Read operations acquire the repository append/rotation lock only long enough to pin a verified checkpoint plus generation high-water, then read that immutable snapshot without blocking later appends.

Every original attempt, same-key retry recovery, online recovery, and startup
transaction recovery acquires the complete normal rank
envelope in that order—repository, bundle **exclusive**, every sorted target
concept, lineage, endpoint, transaction, intent/history, then activity/outbox—
before it classifies or changes transaction authority. It does not acquire a
reduced “recovery” lock set. Recovery for a transaction is serialized across
processes; the exclusive bundle rank prevents direct writers and shared capture
from racing target classification, exchange-back, or roll-forward. Startup keeps
proposal serving closed until all recovery envelopes finish or a blocking marker
is durable.

Under all locks the coordinator re-reads current bytes, tokens, capabilities, config, `validation_context_digest`, expiry, dependencies, runtime identities, and validation authority; captures every exact canonical before identity; loads the sole verified preview; independently restages and verifies all reviewed bytes; writes exclusive per-target stage/backup files; preassigns all finalization ids, payloads/templates, timestamps, and retention; writes/fsyncs/rereads/verifies canonical `transactions/<transaction_id>/intent.json`; writes/fsyncs/rereads/verifies canonical prepared `history.json` and its intent linkage; fsyncs every file and transaction directory; writes and verifies `prepared` WAL with the immutable `transaction_intent_digest`; and only then publishes and verifies the prepared endpoint result. The durable order is therefore **intent → prepared history → prepared WAL → prepared endpoint result**. A crash after WAL and before endpoint preparation is a recoverable pre-decision rollback; endpoint-prepared/no-WAL is forbidden and is treated as orphan contradiction, never as authority for a fresh transaction. Only after all four authorities verify may the coordinator write and verify `commit_decided` before any canonical publication.

Publication is Linux descriptor-relative only. Known okf-loom writers are
cooperative and lock-coordinated through the bundle-exclusive rank; this is the
only prevention claim. For an existing target, the
coordinator verifies the exact before and staged identities through pinned dirfds,
invokes the test-only `before_exchange_syscall` seam only after those final checks,
then uses `renameat2(..., RENAME_EXCHANGE)` between the private stage leaf and
canonical leaf. For a proved-absent create target it uses descriptor-relative
`renameat2(..., RENAME_NOREPLACE)` (or an equivalently fail-closed atomic rename
that cannot overwrite an appearing leaf). Immediately after the syscall and
before any WAL advance it reopens both names no-follow: the destination MUST be
the staged inode and exact bytes with its complete resulting identity, and after
exchange the source MUST be the exact former before inode and bytes. Parent
directories are fsynced and both observations are repeated. Same bytes at a
different inode, or the expected inode at a substituted parent, is a contradiction.

Before `commit_decided`, if recovery proves an exchange occurred, it may exchange
back only when both names and complete identities exactly form the recorded
before/staged pair; it then fsyncs and reverifies the restored before identity.
Otherwise it preserves evidence and fails conflicted/corrupt. At or after
`commit_decided`, it never exchange-backs: it preserves evidence, blocks serving
for the affected aggregate, and rolls forward only from the exact verified staged
authority. Missing, unknown, same-byte/different-inode, or foreign source or
destination identity becomes corruption/`indeterminate_outcome`. A same-UID
malicious writer acting outside the locks can race any userspace check and may
cause brief exposure of bytes or an indeterminate outcome; without privilege
separation this protocol does not claim to prevent that hostile atomic
interference. Exact source, destination, parent, and resulting-identity checks
immediately after each publication syscall and again after parent fsync are
mandatory detection seams. Any contradiction is never silently accepted: it
preserves evidence, writes the applicable marker/tombstone, and blocks proposal
serving for the affected authority until explicit repair.
Proposal Review capture/apply coordinated readers acquire shared ownership; current integrated source/config writers already acquire exclusive bundle transaction ownership. Ordinary current readers remain outside this claim. Therefore the coordinated Proposal Review observer does not see a partial writer set, while uncoordinated readers retain the limitation below.
Uncoordinated external filesystem readers may observe rename progress; the protocol does not claim filesystem-wide multi-file atomicity.

For exactly one target, the verified all-false `commit_decided` WAL is replaced
directly by a durably written and verified `content_published` WAL with bitmap
`[true]`, but only after descriptor-relative publication, immediate source and
destination identity verification, parent-directory fsync, repeat verification,
and exact resulting-identity capture. No `publishing` state is written. For two or
more targets, each completed non-final replacement advances `publishing` to a
durable non-empty proper-prefix bitmap; the final replacement, parent fsync,
  and exact resulting-identity verification replace that proper-prefix WAL directly
with `content_published` and the all-true bitmap. Multi-target publication MUST
NOT skip its proper-prefix `publishing` state. Phase then becomes `finalizing`.
The coordinator verifies the already-durable intent → immutable-history → WAL association, leaves `history.json` byte-for-byte unchanged, performs only the §5.6 deterministic substitutions from exact WAL resulting identities into terminal candidates, then durably appends the exact candidate transaction audit, activity rows, pending outbox receipt, and terminal endpoint result; marks WAL `committed`; fsyncs; attempts idempotent event append; and only then returns the endpoint result. Existing byte-identical candidate rows count as complete; an identity match with different bytes is corruption.
WAL retirement is permitted only after committed WAL, finalization intent, endpoint result/tombstone, transaction audit, history manifest, activity links, and outbox receipt mutually verify. The retained audit repeats both `transaction_intent_digest` and `history_manifest_digest` so retirement does not sever the chain.
After WAL retirement, restart and undo locate the applied transaction through transaction audit → `transaction_intent_digest` → canonical intent path → `history_manifest_digest` → canonical history path → exact backup identities; any missing/mismatched link returns `storage_corrupt` before restore.
The product commit-decision boundary is durable `commit_decided`: before it recovery restores/stays at all before bytes; at or after it recovery completes all forward publication and metadata.
`content_published` records completion, not intent.

| Crash point | Durable observation | Recovery action | Retry result | Proof |
|---|---|---|---|---|
| Before WAL | no publish authority; intent/history may be absent, partial, or a verified pair | verify no canonical change; delete only recognized prepare artifacts whose recorded path, type, owner, mode, link count, complete identity, and transaction linkage match; preserve every unknown entry as evidence | start normally, or block serving and fail closed on an unknown/contradictory entry | `CRASH-01` |
| During stage/backup/intent/history before prepared | no WAL | verify no canonical change; delete only recognized identity-bound prepare artifacts, never synthesize a missing pair member; unknown files, links, or directories are retained and block serving | start normally or fail closed | `CRASH-02` |
| After prepared WAL, including before prepared endpoint publication | commit false, all-false maps; endpoint may be absent or exact prepared | acquire the complete recovery lock envelope, enter rolling_back, verify/restore exact before identities, then publish/finalize the intent-bound rejected result | same key replays rejection | `CRASH-03` |
| During durable `commit_decided` replacement | old valid prepared or new valid commit_decided | old rolls back; new rolls forward; invalid/torn authority creates corruption marker | replay determined by recovered record | `CRASH-04` |
| After `commit_decided`, before first rename | commit true, all-false publish | verify pinned parents/stage and begin forward publication | same key waits/replays | `CRASH-05` |
| After exchange/rename before source/destination verification or parent fsync | commit true; bitmap may lag | classify both names by complete identity, never digest alone; if the exact exchange/rename is proved, fsync and reverify both ends before advancing; if exact before remains, republish only from verified staged authority | roll forward or indeterminate | `CRASH-06` |
| During multiple exchanges/renames | verified durable proper prefix or lagging exact identity pairs | under the complete recovery lock envelope reconcile each target only as the exact before/staged/resulting identity state; record each resulting identity with the next durable proper prefix; same-byte/different-inode or any foreign identity → indeterminate | roll forward or indeterminate | `CRASH-07` |
| After all exchanges/renames before `content_published` | all exact resulting identities; WAL is `commit_decided` with `[false]` for one target or `publishing` with a proper prefix for multiple targets | fsync every affected parent, reverify every exact source/destination/resulting identity, capture it in WAL, then durably replace WAL with `content_published` and the all-true bitmap; never synthesize a multi-target direct transition | eventual success replay | `CRASH-08` |
| After `content_published`, before finalization rows | publication complete; verified intent/immutable history exist | leave history unchanged; substitute verified WAL outcomes into exact intent candidates and append missing audit/activity/outbox/result bytes idempotently | eventual exact success replay | `CRASH-09` |
| During finalization, terminal authority missing, or intent/history missing/corrupt | candidate prefix may exist; association is either fully verified or unsafe | reconstruct only exact intent-bound audit/activity/outbox/result candidates when all immutable-history and upstream WAL identities/digests verify; otherwise before `commit_decided` roll back/fail closed, and at/after it write corruption marker + indeterminate tombstone and preserve evidence—never replace history, recreate aggregate authority, or infer from current bytes | exact replay when verified; otherwise rejection before decision or `409 indeterminate_outcome` after decision | `CRASH-10` |
| After endpoint result before WAL committed | terminal endpoint authority exists | verify result, mark WAL committed | exact replay | `CRASH-11` |
| After committed before event append | result + pending receipt | append/re-observe event by outbox id; at-least-once | exact replay; clients refetch | `CRASH-12` |
| After event append before receipt update | event may exist, pending receipt | detect by outbox id or append duplicate notification; mark appended | exact replay; client dedupe | `CRASH-13` |
| After receipt before response | complete authorities | no canonical mutation | exact replay | `CRASH-14` |
| During pre-decision rollback | commit false + reverse rollback prefix | finish reverse restoration and mark rolled_back | rejected replay | `CRASH-15` |
| During/after decision with missing/substituted private/canonical identity, including same-byte inode substitution | commit true but evidence unsafe | preserve everything, marker + indeterminate tombstone, block aggregate; never accept digest-only equivalence | `409 indeterminate_outcome` | `CRASH-16` |

Endpoint replay reconciliation precedence is exact: (1) valid terminal endpoint result wins only after its intent/audit linkage verifies, and its response is replayed; (2) absent result plus valid `terminal_compacted` tombstone returns the compacted terminal envelope; (3) absent result plus indeterminate tombstone returns `indeterminate_outcome`; (4) absent terminal authority plus coherent nonterminal intent+WAL+history resumes/reconciles that transaction; (5) transaction audit without endpoint result may append only the exact intent candidate when intent, WAL/history association, fingerprint, result id, and complete response digest all verify, otherwise it writes an indeterminate tombstone; (6) no authority permits a new transaction only when no intent, WAL, audit, result, or tombstone exists for the key and fingerprint.
Any contradiction creates a corruption marker and blocks mutation.

### 8.1 Apply/undo failure wire and persistence matrix

In this matrix, compact `409 code_a|code_b` notation enumerates exact alternative status/code pairs; the vertical bar is never part of a code.

| Authority/phase | Public wire | Durable endpoint result | Transaction/WAL | Replay behavior | Read-model attempt |
|---|---|---|---|---|---|
| Guard/schema/quota/CAS/conflict before endpoint prepare | The exact first-matching §10 error (`400 invalid_host|invalid_json`, `401 invalid_session_token`, `403 origin_forbidden|read_only|proposal_mutation_loopback_only`, `404 not_found|capability_unavailable|proposal_not_found`, `405 method_not_allowed`, `413 body_too_large`, `415 unsupported_media_type`, `422 invalid_request|validation_failed`, the named §4 `429`/`507` quota code, `503 bundle_busy|lock_unavailable|storage_corrupt|preview_authority_unavailable`, or `409 invalid_state|stale_read_model|proposal_conflict`) | none | none | retry re-evaluates with a new or same unused key | none |
| Endpoint `prepared`, no WAL | `503 storage_corrupt` or `409 indeterminate_outcome` as safely classifiable | orphan prepared authority, never normal state | forbidden ordering contradiction | same key never starts a fresh transaction; recovery marks/blocks | none |
| WAL `prepared`, endpoint absent or prepared | `503 transaction_recovering` while endpoint is absent; otherwise `202 transaction_in_progress` | absent or prepared | nonterminal prepared bound to verified intent/history; crash after WAL before endpoint rolls back | same key recovers the same transaction to rejected terminal; association mismatch fails closed | `preparing` |
| WAL `rolling_back` | `202 transaction_in_progress` result | prepared | nonterminal rollback | same key returns 202 | `rollback_pending` or `recovering` |
| WAL `commit_decided` | `202 transaction_in_progress` result | prepared | forward decision durable | same key returns 202; never rollback | `forward_committed` or `recovering` |
| WAL `publishing` | `202 transaction_in_progress` result | prepared | forward prefix | same key returns 202 | `publishing` or `recovering` |
| WAL `content_published`/`finalizing` | `202 transaction_in_progress` result | prepared | publication complete, metadata pending | same key returns 202 | `finalizing` or `recovering` |
| Terminal success | `200` transaction result with status `applied` or `undone` | terminal exact success envelope | committed audit; WAL may later retire | same key replays exact 200 with `replayed:true` | none; `apply_result`/`undo_result` present |
| Terminal pre-decision error | Exact persisted error, e.g. `409 apply_rejected`, `409 proposal_conflict`, `422 validation_failed`, or `500 contract_parity_failure` | terminal exact error envelope/status | rolled_back audit or no WAL as recorded | same key replays exact status/body | none; audit exposes failed terminal attempt |
| Indeterminate | `409 indeterminate_outcome` error envelope | indeterminate result or tombstone | indeterminate audit/marker | every same-key replay remains 409 | `indeterminate` until repaired, then terminal audit projection |
| Storage corruption without classified transaction outcome | `503 storage_corrupt` error envelope | existing valid terminal result is preserved; otherwise no invented result | marker blocks aggregate | same key cannot advance until repair | no synthetic attempt; read fails with corruption id |
| Terminal result with pending notification | Same exact terminal `200` or error | terminal | committed audit + pending outbox | exact terminal replay; notification may repeat | terminal result only, no progress |

The nonterminal response is exactly `{schema:1,data:{status:"transaction_in_progress",attempt:<transaction-progress-projection>,read_model_token}}`, HTTP `202`, and `Retry-After: 1`; it is not persisted as a terminal response.
After generic guard/body/schema checks, apply/undo idempotency precedence is: terminal endpoint result → terminal/indeterminate tombstone → nonterminal WAL/attempt 202 → prepared endpoint recovery → current-resource preflight.
No terminal error is represented as a success result and no 202 is represented as an error envelope.

## 9. Storage, integrity, rotation, and migration

All paths are relative to the configured Studio session root, opened through one pinned trusted dirfd:

```text
proposal-review-v1/
  repository.json
  active/{proposals,decisions,transactions,endpoint-results,outbox,tombstones,corruption}.jsonl
  checkpoints/{proposals,decisions,transactions,endpoint-results,outbox,tombstones,corruption}.json
  generations/<kind>-00000000000000000001.jsonl
  service-wal/<intent_id>.json
  preview-staging/<intent_id>/preview.json
  previews/sha256/<first-two-hex>/<remaining-hex>.json
  transactions/<transaction_id>/{intent.json,history.json,wal.json,stage/,backup/}
  quarantine/
```

One `ProposalRepositoryV1` owns every path above, all aggregate logs/checkpoints/sequences/generations, service-intent WALs, apply/undo finalization intents, preview staging and promotion, previews, endpoint results, tombstones, rotation, quota accounting, markers, and startup recovery.
No route, projector, coordinator, or legacy reader writes these paths independently.
Withdrawal is a proposal aggregate record; apply and undo are transaction aggregate records.

`service-wal/` and `preview-staging/` are private directories with mode exactly `0700` or `02700`. Service-intent files are mode `0600`, link count 1, use the exact canonical `service-wal/<intent_id>.json` path, and are published/advanced with the §5.6A atomic replacement protocol. They share the repository aggregate quota and additionally enforce the §4 service-WAL count/byte/per-file limits under the append/rotation lock. The repository reserves enough quota for one required indeterminate advancement before admitting a new intent; if it cannot, it rejects admission with `507 repository_quota_exceeded` before endpoint preparation.

`service-admission.json` is repository-owned hard authority for the session-global create/supersede token bucket and preview worker/queue leases. It is closed, checksummed, atomically replaced under the repository lock, and never delegated to a `ProposalService` process-local counter. Each preview lease records its lease id, Linux owner PID, kernel process-start token, and heartbeat expiry; PID alone is never liveness authority. Preview leases are bounded to two active and eight queued identities. A live actual worker renews before expiry even after caller timeout and retries transient renewal failures with bounded backoff. An expired heartbeat may be reclaimed only when `/proc` proves the PID dead or its start token mismatched; unknown liveness retains the lease fail-closed. Its done callback stops renewal and releases only after worker exit. Process death stops renewal and safely proved expiry reclaims the lease. A process-local semaphore or soft rate limiter may exist only as a non-authoritative optimization.

Startup validates and recovers every service intent before proposal reads or mutations become available. Recoverable phases roll forward exact stored domain/outbox/terminal rows to `committed`; contradictions become durable indeterminate authority and corruption markers. A certain committed intent may be pruned only after the exact 180-day audit horizon, once its domain and outbox/result bindings remain provable from retained authority, no preview or corruption reference requires it, and parent-directory fsync completes. Nonterminal, referenced, and indeterminate intent files are never age-pruned. Service intent retention is independent of the seven-day endpoint replay and 30-day preview-byte horizons and the apply/undo transaction WAL.

`repository.json` is the repository root authority and has exactly `{schema:1,repository_id,created_at,aggregate_versions,cursor_keys,active_cursor_key_id,limits_digest,checksum}`.
`aggregate_versions` is exactly `{proposals:1,decisions:1,transactions:1,endpoint_results:1,outbox:1,tombstones:1,corruption:1}`.
`cursor_keys` is an array of 1–3 entries `{key_id,secret_base64url,created_at,not_before,expires_at}`; secrets decode to exactly 32 bytes, key ids are `cur_` plus 26 Crockford characters, intervals cannot overlap ambiguously for one key id, and `active_cursor_key_id` names exactly one currently valid key.
`created_at` and every key time are non-null canonical timestamps; `not_before < expires_at`; repository id, active key id, limits digest, and checksum are non-null strings using their declared primitives.
`limits_digest` is the Digest of canonical JSON over the exact effective §4 named repository, checkpoint, tombstone, quarantine, audit, active-proposal, lineage, rate, preview-worker, and generation-count limits as an object sorted by limit name; no operational limit may be omitted.
`checksum` is constructed by removing only the top-level `checksum` member, canonical-JSON encoding the complete remaining object—including cursor-key secrets—as UTF-8 under §3, hashing those exact bytes with SHA-256, and serializing the result as a Digest; nested fields are never omitted or redacted for checksum construction.
The file is mode `0600`, link count 1, atomically replaced and parent-fsynced; missing, unknown-version, duplicate-key, invalid-key-ring, quota-digest mismatch, or checksum failure prevents proposal repository startup.

The directory is self-ignored with `.gitignore` containing `*` and mode exactly `0700` or `02700`; regular records use `0600`.
The trusted starting descriptor is the already contained, non-symlink bundle-root dirfd established by the current server; an absolute externally supplied session path is forbidden in v1.
The configured bundle-relative session root is opened component-by-component from that descriptor using `openat` with no-follow behavior; every existing component must be a real directory owned by the effective uid, not group/other writable, and have stable device/inode across open and use.
The repository root and private directories require effective-UID ownership and
mode exactly `0700` or `02700`; authority files require regular-file type, owner
uid, mode `0600`, and `st_nlink == 1`. This is an exact allowlist, not a mask that
ignores special bits: group/other access, sticky, and setuid remain forbidden.
Directory setgid affects group inheritance only, so an inherited GID need not
equal the effective GID. Private directories are requested as `0700` without
chmod of any newly created or existing state. The same policy applies during
bootstrap, traversal, publication, recovery, and private-directory removal.
Admission may reopen safe caller-owned `02700` state, including replacement
before the first observation; it does not claim creation provenance or exclude
hostile same-UID writers. No-follow, descriptor pinning, and observed identity
checks remain in force. See the current specification's
[private-directory admission contract](/reference/spec.md#private-directory-admission)
for the trust assumptions and unchanged ancestor policy.
Concept parent directories are pinned by dirfd before staging and remain pinned through every publish/fsync; device/inode must match the WAL target identity before each rename.
Stage and backup leaves are created exclusively (`O_CREAT|O_EXCL` equivalent), no-follow, mode `0600`, link count 1, then pinned and revalidated by device/inode/size/digest before use.
Any symlink, hard link, owner/mode mismatch, component or leaf substitution, parent replacement, inode/device drift, unexpected existing stage name, or link-count change fails before commit decision; at/after commit decision it creates a corruption marker and indeterminate outcome unless exact safe roll-forward is provable.
Paths are descriptor-relative, no-follow, regular-file only, with post-open identity and containment checks.
Leaf names are derived only from validated ids or digests.

JSONL records are full records, one canonical JSON object plus LF.
Every record includes a checksum over its content excluding checksum where its schema specifies one.
An incomplete final line is truncated under the append lock before the next append.
A complete malformed row, checksum mismatch, generation gap, checkpoint mismatch, duplicate transaction identity, or ambiguous latest row durably appends a corruption marker and makes the affected aggregate `503 storage_corrupt`; it is optionally copied to quarantine but never skipped to invent state.
Startup recovery completes before proposal routes become available.
It also validates every terminal apply/undo transaction chain, whether its WAL
is retained or retired: intent, immutable history linkage, transaction audit, terminal
endpoint result or tombstone, activity ids, and outbox receipts MUST mutually
identify the same transaction, fingerprint, candidates, before identities, and
resulting identities. A missing or corrupt terminal association is regenerated
only when the exact closed candidate is reproducible from verified intent-bound
authority; otherwise startup writes the applicable corruption/indeterminate
authority and keeps serving blocked. For an initialized repository,
`repository.json`, any declared aggregate active log/checkpoint/generation, and
required aggregate sequence authority are never silently recreated as empty when
missing. Missing aggregate authority is corruption requiring explicit repair.
For every apply/undo transaction directory and every associated endpoint authority,
startup validates the intent checksum and digest, WAL
`transaction_intent_digest`, operation/transaction/parent identity, history
digest, and exact target/backup linkage before choosing a phase action. If
intent or history is missing, malformed, corrupt, or mismatched while the last
verified WAL is absent or before `commit_decided`, recovery performs only a
provably safe rollback/stay-before path, preserves diagnostic authority, and
fails closed if rollback cannot be proved. At or after `commit_decided`, the
same defect durably records corruption plus an indeterminate endpoint
tombstone, preserves every file and candidate prefix, and blocks automatic
mutation. Recovery never fabricates either durable file or any finalization
row.

An active log rotates at 8 MiB into a 20-digit monotonic generation.
At most eight generations remain after a verified checkpoint covers all records to be pruned.
Checkpoint is written to a temporary, fsynced, atomically renamed, parent-fsynced, then reread and verified before generation deletion.
Any checkpoint, rotation, fsync, or prune failure retains old data and fails the write closed.
Pruning obeys every horizon and quota in §§4 and 7.5 and never prunes an intent or other authority referenced by a nonterminal WAL, uncertain endpoint result/tombstone, corruption marker, retained history/undo artifact, or retained audit link. A terminal transaction intent may be deleted only after WAL retirement, replay and undo expiry, audit compaction to sufficient retained linkage, and proof that no result/tombstone/audit/corruption/repair reference requires its exact bytes; every deletion is ordered and parent-directory-fsynced. Intent quota pressure fails new apply/undo admission and never evicts live authority.
Pagination cursors are stateless and create no retention pin; pruning may invalidate a cursor, which then fails `409 cursor_expired` rather than omitting rows.
Checkpoint creation uses one repository sequence high-water captured under the append/rotation lock; readers merge that exact checkpoint with generations through that high-water and never mix a later aggregate generation into the same read snapshot.
Operator repair is the exact offline repair-row/successor-marker protocol in §5.7; deleting or quarantining the bad row alone is not repair.

Intent publication uses exclusive temporary creation beside `intent.json`, file fsync, atomic rename, transaction-directory fsync, reread, checksum/digest/closed-schema verification, and operation-specific semantic verification. History publication then uses the same protocol beside `history.json`, with digest and target linkage verified against that intent, before `prepared` WAL.
Recovery before `commit_decided` may roll back and may delete only recognized,
identity-bound private prepare artifacts after rolled-back audit/result durability
and evidence-preservation checks. Unknown files, links, directories, types, or
identities are preserved and block serving; cleanup never guesses ownership from
a filename. Recovery at/after `commit_decided` MUST preserve intent and immutable
history bytes exactly. Missing, corrupt, or mismatched intent/history is corruption
and indeterminate; neither file is reconstructed or replaced from current proposal
state, audit fragments, canonical target bytes, or any other insufficient state.
The manifest and backup leaves remain authoritative for undo through `undo_expires_at` and cannot be pruned while a nonterminal apply/undo, active undo request, uncertain authority, or corruption marker references them.
After undo expiry and terminal verification, backups and history may be deleted in that order with directory fsync; the 180-day transaction audit retains manifest digest and backup identities as audit evidence but explicitly projects undo unavailable.

Repository conformance requires atomic/checksummed `repository.json`, every aggregate/checkpoint rule, repair protocol, sole preview authority, endpoint-result/tombstone replay, and intent/history publication/linkage/retirement/restart/pruning contracts to pass `STORE-01..13`; partial conformance MUST NOT activate the capability.

Existing `proposals.jsonl` suggestion rows are read through a separate bounded compatibility reader and projected only as `legacy-suggestion`.
They cannot receive decisions, preview, apply, or undo.
There is no automatic upgrade.
Explicit regeneration supplies the closed identity `{legacy_id,source_generation}` in API body, CLI options, and provenance.
The CLI form is `proposal create --regenerate-legacy-id <id> --legacy-source-generation <n> --mode proposal`; both are required together.
The server verifies the exact legacy row identity immediately before creating the independent v1 proposal.
Reading or migration never rewrites legacy storage.
For the retained Studio stream, active `proposals.jsonl` is generation `0`;
rotated `proposals-YYYYMMDD-NN.jsonl` generations are the integer
`YYYYMMDDNN`. Missing/pruned generation, duplicate identity, malformed complete
row, or changed projection fails closed before intent admission.

### 9.1 Endpoint idempotency outside apply/undo

Proposal create/supersede, withdrawal, and decision mutations use endpoint-result/tombstone replay authority plus the metadata-only service mutation WAL in §5.6A; they never use or imply the apply/undo content WAL.
Create/supersede and withdrawal require `Idempotency-Key`; decision accepts a key and, when omitted, the server generates an internal one-shot key/intent identity but the client receives no blind-retry guarantee after an ambiguous transport failure.
Create/supersede fingerprints include protocol version, exact route scope, canonical request body, session repository id, current base proposal authority, and relevant runtime identities. Decision/withdraw fingerprints use the same inputs but omit `read_model_token` exactly as §5.6A requires; the accepted token is bound inside the intent. Fingerprints never include the CSRF token or treat actor/author labels as principals.

Reconciliation precedence is exact and occurs under the key and repository locks: (1) terminal endpoint result; (2) terminal/indeterminate tombstone; (3) coherent service intent in any phase, which is recovered and resumed; (4) a prepared endpoint result without coherent matching intent, which becomes durable indeterminate; (5) only complete absence of result, tombstone, intent, and matching domain/outbox authority permits evaluation of current state/CAS and preparation of a new intent. Current stale-token, lifecycle, lineage, rate, and quota checks MUST NOT precede same-key reconciliation. A coherent intent's exact stored terminal response wins over later mutable state.

| Endpoint | Prepared authority | Crash reconciliation | Retention/replay |
|---|---|---|---|
| Create/supersede | coherent service intent prepared with assigned revision, staged/promoted preview refs, exact proposal/outbox/result bytes | roll forward those exact bytes; missing intent beside prepared endpoint becomes indeterminate; supersede rechecks unique current predecessor under final lock | exact endpoint result 7 days; sufficient terminal tombstone and intent/audit binding through 180-day audit horizon; preview bytes independently 30 days |
| Keyed decision | coherent intent prepared with exact decision row, accepted token, notification, and terminal response | matching intent/row resumes before stale-token evaluation; mismatch marks corruption/indeterminate | exact endpoint result 7 days, then tombstone/intent binding through 180-day audit horizon |
| Keyless decision | same service intent under repository-private one-shot key identity | matching row/intent finalizes; after lost response client MUST canonical-refetch and issue new intent with latest CAS, never blind retry | internal exact result 24 hours; intent/audit binding 180 days |
| Withdrawal | coherent intent prepared with exact withdrawal row, accepted token, notification, and terminal response | matching intent resumes before stale-token evaluation; contradiction marks corruption/indeterminate | exact endpoint result 7 days, then tombstone/intent binding through 180-day audit horizon |

Same scope/key/fingerprint replays the exact success or error; changed non-token fingerprint returns `idempotency_conflict`.
Endpoint result retention is seven days from terminal creation and is not extended to the 30-day preview horizon. At compaction, a certain outcome becomes a tombstone carrying enough digest/identity linkage to prevent reuse through the 180-day audit horizon; uncertain authority becomes an indeterminate tombstone for that whole horizon. Neither compaction nor preview expiry authorizes duplicate creation, supersession, decision, or withdrawal.

## 10. HTTP API contract

All routes require an allow-listed non-empty `Host` and `X-OKF-Token`, including GET.
`X-OKF-Token` is a session CSRF capability, not user authentication; `author` and `actor` are unauthenticated labels.
Mutating routes additionally require the activation-rotated high-entropy
`X-OKF-Proposal-Token`. It is stored only as private `0600` session authority
and MUST NOT appear in ordinary Studio HTML, events, URLs, referrers, or logs.
Possession of HTML and `X-OKF-Token` through an unmanaged reverse proxy is
therefore insufficient to mutate proposals. This second token is a local
mutation capability, not remote user authentication; a future browser decision
or apply UI requires a separately specified local-only bootstrap/listener. The
inspection-only Studio Proposals panel reads with `X-OKF-Token` alone and never
receives this token.
Mutating routes additionally require an `Origin`, when present, whose canonical
scheme, normalized host, and effective port exactly equal a configured served
origin. Null, malformed, userinfo-bearing, path/query/fragment-bearing,
wrong-scheme/port, and ambiguous IPv6 origins fail closed.
Every proposal request target is byte-capped before URL parsing, strictly
percent-decoded as UTF-8, and bounded to three closed query fields. Every HTTP
method under the reserved prefix routes through this adapter; unsupported
methods return the closed `405 method_not_allowed` envelope.
After route/CSRF/Origin checks, `--no-edit` permits list/show but returns `403 read_only` for create, decision, withdraw, apply, and undo before reading/parsing JSON, checking socket locality, or creating idempotency state.
Otherwise every mutating proposal route requires the accepted socket local and remote addresses to be loopback and the server to have no public/tunnel edit exposure; failure returns `403 proposal_mutation_loopback_only` before body parsing or idempotency state.
Every proposal response, success or error, carries `Cache-Control: no-store`, `Pragma: no-cache`, `Referrer-Policy: no-referrer`, and `X-Content-Type-Options: nosniff`.

| Method and path | Request | Success | Idempotency | Proof |
|---|---|---|---|---|
| `POST /__proposal/v1/proposals` | create/supersede body §5.1, optionally exact `legacy_identity` | `201` proposal read model | required key | `HTTP-01` |
| `GET /__proposal/v1/proposals?state=&cursor=&limit=` | strict query | `200` page | read-only | `HTTP-02` |
| `GET /__proposal/v1/proposals/{id}/revisions/{rev}` | no body | `200` read model | read-only | `HTTP-03` |
| `POST /__proposal/v1/proposals/{id}/revisions/{rev}/decisions/{op_id}` | `{schema,decision,reason,previous_row_token,read_model_token}` | `200` decision + new tokens | optional key | `HTTP-04` |
| `POST /__proposal/v1/proposals/{id}/revisions/{rev}/withdraw` | `{schema,read_model_token,reason}` | `200` read model | required key | `HTTP-05` |
| `POST /__proposal/v1/proposals/{id}/revisions/{rev}/apply` | apply-v1 body | `200` applied/replay | required key | `HTTP-06` |
| `POST /__proposal/v1/proposals/{id}/revisions/{rev}/undo` | `{schema,transaction_id,undo_group_id,read_model_token}` | `200` undone/replay | required key | `HTTP-07` |

List order is descending `(created_at,proposal_id,proposal_revision)`.
`state` is optional and repeat-forbidden.
The closed cursor payload is exactly `{schema:1,repository_id,query_digest,last_created_at,last_proposal_id,last_proposal_revision,repository_high_waters,bundle_snapshot_digest,validation_context_digest,clock_captured_at,earliest_expiry_at,environment_digest,cursor_expires_at}`.
`repository_high_waters` is exactly `{proposals:{generation,sequence},decisions:{generation,sequence},transactions:{generation,sequence},endpoint_results:{generation,sequence},outbox:{generation,sequence},tombstones:{generation,sequence},corruption:{generation,sequence}}`, with non-negative safe JSON integers.
The projection snapshot inputs are exactly those high waters, bundle snapshot digest, `validation_context_digest`, captured clock instant, earliest included expiry, query/filter digest, config digest, active capability digest, `validator_id`, `serializer_id`, and `operation_runtime_id`; `environment_digest` hashes config, capability, validation-context, and runtime identities.
The three `last_*` fields are the final emitted item's non-null sort tuple; an empty/final page emits `next_cursor:null` rather than a cursor with null boundaries.
`earliest_expiry_at` is the earliest non-null retention/lifecycle expiry among frozen items or null when none exists; cursor expiry is exactly `min(clock_captured_at+10 minutes, active-key expires_at, earliest_expiry_at when non-null)`.

Cursor encoding is exact: canonicalize the payload to UTF-8 bytes `P`; compute `T=HMAC-SHA-256(key, "okf-proposal-cursor-v1\0" || repository_id || "\0" || P)`; construct canonical JSON envelope `{v:1,kid,payload,tag}` where `payload` is unpadded base64url `P` and `tag` is unpadded base64url 32-byte `T`; return unpadded base64url of the envelope bytes.
Decode rejects padding, non-canonical base64url, unknown envelope keys/version, unknown/expired key id, non-canonical reserialized payload, repository mismatch, tag mismatch using constant-time comparison, query mismatch, or invalid field bounds.
Cursor keys are scoped to one `repository_id` and this route family only.
The repository rotates the active key at most every 24 hours, creates the successor before use, retains retired keys through the maximum 10-minute cursor lifetime plus 60 seconds, and never accepts a cursor after either payload expiry or key expiry.

Each page is exactly `{schema:1,items,next_cursor,page_snapshot_token,snapshot_expires_at}`.
`page_snapshot_token` hashes the complete frozen projection inputs and item sort boundaries; it is display/pagination consistency evidence only and MUST NOT authorize proposal mutation.
Each proposal item retains its own per-proposal `read_model_token`, which alone participates in decision/withdraw/apply/undo CAS.
Pagination freezes all derived inputs named in the cursor.
State filters are evaluated at the captured clock instant; concurrent appends, bundle changes, runtime changes, and later expiry do not alter later pages.
If any required generation or preview authority is pruned or corrupt before completion, the cursor fails `cursor_expired`/`storage_corrupt`; it never silently skips.
Continuation is reconstructed on every request from the signed `repository_high_waters` and the immutable checkpoint/generation, bundle-snapshot, validation-context, environment, clock, expiry, query, and sort-boundary facts carried or digested by the cursor. Any worker or restarted process with the same repository key ring and still-retained matching facts MUST produce the same continuation. A process-local view, object cache, cursor registry, sticky session, or prior worker is neither required nor authoritative. Implementations MAY cache verified immutable inputs only as an optimization and MUST prove byte-identical reconstruction after clearing the cache. If bundle/runtime/environment facts no longer match, or a required checkpoint, generation, preview, key, or frozen expiry fact was changed or pruned, continuation returns `409 cursor_expired` with `resync_required:true`; it MUST NOT silently substitute current facts or report a cache miss as not found.
Items sort by unique tuple `(created_at,proposal_id,proposal_revision)` descending, and the exclusive last tuple ensures no duplicate or skip.
A malformed/tampered cursor returns `400 invalid_cursor`; expired key/payload or pruned frozen authority returns `409 cursor_expired` with `resync_required:true`.
The cursor is stateless server-side and creates no active authority or prune pin.

The read-model token is the proposal-specific digest defined in §5.7. Aggregate high waters and unrelated outbox/endpoint authority do not participate; `page_snapshot_token` and signed cursors continue to bind aggregate high waters.
Every mutation rechecks it under locks.
A stale token returns `409 stale_read_model` and the client refetches; it never merges stale controls.

Success and error envelopes are never bare values.
Every success, including `201` and replay, is exactly `{schema:1,data:<closed route result>}`; replay state is inside the closed result where applicable and no additional top-level keys are allowed.
Errors are:

```json
{
  "schema": 1,
  "error": {
    "code": "stale_read_model",
    "message": "Canonical proposal state changed; refetch before retrying.",
    "request_id": "req_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
    "retryable": false,
    "resync_required": true,
    "details": []
  }
}
```

| Precedence | Condition | Status/code | Proof |
|---:|---|---|---|
| 1 | Invalid/missing Host | `400 invalid_host` | `ERR-01` |
| 2 | Missing/invalid CSRF capability | `401 invalid_session_token` | `ERR-02` |
| 2a | Invalid/oversized proposal request target | `400 invalid_request_target` or `414 request_target_too_large` | `ERR-02` |
| 3 | Disallowed/non-canonical Origin | `403 origin_forbidden` | `ERR-03` |
| 4 | Unknown route/method/capability | `404 not_found` or `404 capability_unavailable`; `405 method_not_allowed` for known path | `ERR-04` |
| 5 | Mutation under no-edit | `403 read_only` | `ERR-05` |
| 5a | Missing/invalid local proposal mutation capability on a mutation | `401 invalid_proposal_token` | `ERR-05` |
| 6 | Mutation under non-loopback/public/tunnel editable exposure | `403 proposal_mutation_loopback_only` | `ERR-06` |
| 7 | Body exceeds route cap | `413 body_too_large` | `ERR-07` |
| 8 | Wrong content type/malformed JSON/duplicate key | `415 unsupported_media_type` or `400 invalid_json` | `ERR-08` |
| 9 | Closed-schema/type/limit/path/dependency error | `422 invalid_request` | `ERR-09` |
| 10 | Rate/quota/lock/storage unavailable | typed `429`, `507`, or `503` | `ERR-10` |
| 11 | Idempotency key/fingerprint conflict or indeterminate | `409 idempotency_conflict` / `409 indeterminate_outcome` | `ERR-11` |
| 12 | Resource missing/expired/invalid state | `404 proposal_not_found`; `410 preview_expired`; `409 invalid_state` | `ERR-12` |
| 13 | CAS, snapshot, decision, conflict, validation failure | `409 stale_read_model`, `409 proposal_conflict`, or `422 validation_failed` | `ERR-13` |
| 14 | Unexpected internal/parity failure | `500 internal_error` / `500 contract_parity_failure` | `ERR-14` |

Host, CSRF-capability, Origin, no-edit, and loopback checks deliberately mask lower-level body, existence, storage, and path details; they do not establish a user identity.
Retry automatically only `429`, `503`, and transport timeout, with the identical key/body and the bounded client policy in §12.
Future remote proposal mutation requires a separately specified authenticated principal and authorization model; v1 has no remote-mutation escape hatch.

## 11. CLI contract

The matching commands are:

```bash
scripts/okf-loom proposal create <bundle> --mode proposal --file proposal.json --idempotency-key KEY --format json
scripts/okf-loom proposal create <bundle> --mode proposal --supersede <id>:<rev> --file proposal.json --idempotency-key KEY --format json
scripts/okf-loom proposal create <bundle> --mode proposal --regenerate-legacy-id ID --legacy-source-generation 0 --file proposal.json --idempotency-key KEY --format json
scripts/okf-loom proposal list <bundle> --state ready --limit 50 --cursor TOKEN --format json
scripts/okf-loom proposal show <bundle> <id> --revision 1 --format json
scripts/okf-loom proposal decide <bundle> <id> --revision 1 --op <op_id> --decision accepted --row-token TOKEN --read-model-token TOKEN --format json
scripts/okf-loom proposal withdraw <bundle> <id> --revision 1 --read-model-token TOKEN --reason TEXT --idempotency-key KEY --format json
scripts/okf-loom proposal apply <bundle> <id> --revision 1 --actor ACTOR --snapshot-digest DIGEST --validation-context-digest DIGEST --decision-set-token TOKEN --read-model-token TOKEN --idempotency-key KEY --format json
scripts/okf-loom proposal undo <bundle> <id> --revision 1 --actor ACTOR --transaction-id TXN --undo-group-id GROUP --read-model-token TOKEN --idempotency-key KEY --format json
```

Legacy regeneration is implemented: both identity flags are required together,
the retained generation is verified immediately before intent admission, and
the independent v1 proposal records canonical legacy identity provenance
without mutating the legacy stream. Proposal apply and proposal undo are
registered experimental commands and use the same service contracts as HTTP.

Commands are non-interactive by default, never infer latest revision, never prompt on stdin unless `--interactive` is explicit, and emit the same schema as HTTP.
JSON goes to stdout; diagnostics go to stderr.
Exit codes are `0` terminal success/replay, `2` local usage/schema, `3` validation, `4` unavailable/not found/expired, `5` conflict/CAS/indeterminate, `6` transport/storage/internal, and `7` nonterminal transaction progress (`data.status: transaction_in_progress`).
CLI calls the same service contracts as HTTP and MUST pass contract-parity fixtures.

## 12. Notifications, async lifecycle, cache, and concurrency

Proposal notifications use the existing `/__events` global strictly increasing cursor and existing subscribe-before-snapshot handoff.
Frames are `proposal` events whose `data` is the notification schema in §5.7.
They invalidate; they never reconstruct proposal state, transfer decisions, or enable apply.
Delivery is at least once. A retained `outbox_id` is reusable only when event
type, exact canonical stored payload, and payload digest all match; mismatch is
storage corruption and leaves the receipt pending. Existing event authority is
owner/type/link-count checked and normalized to `0600` before proposal append.
Eager reconciliation is notification-only: any exception leaves the receipt
pending and cannot replace an already committed HTTP/CLI mutation success.
Clients deduplicate by cursor and `outbox_id` and always refetch canonical state.
Unknown events, cursor gaps, a `resync` frame, cursor older than retention, reconnect, polling takeover, or BFCache restore requires canonical list/read refetch.
Polling uses the current events read route oldest-first with `since` and has identical gap semantics.
In the Public result column, compact notation such as `409 code_a|code_b` enumerates the exact alternatives `409 code_a` and `409 code_b`; the vertical bar is never part of an error code.

| Scenario | Deadline/caller-wait owner | Mutation phase | Public result | Persisted state | Late success | Late error | Retry/reconnect | Cleanup | Planned proof |
|---|---|---|---|---|---|---|---|---|---|
| List/show fetch | Route epoch; 10 s absolute AbortSignal | none | `200` page/read model; otherwise exact `400 invalid_cursor`, `409 cursor_expired`, `503 bundle_busy|storage_corrupt`, or transport timeout | none | discard unless full route/generation key matches | cannot clear canonical health | max 3 timeout/429/503, 250/500 ms+jitter | abort timer/backoff | `ASYNC-01` browser `tests/test_proposal_browser.py::test_async_01_list_generation_fence` |
| Create plus preview | Create generation; 30 s client wait; server preview caller waits at most 60 s | endpoint prepared → staged preview → service intent prepared → promote → proposal/outbox/result → intent committed | `201` read model; `409 snapshot_changed|snapshot_unstable|idempotency_conflict|indeterminate_outcome`; `429 create_rate_limited|preview_concurrency_limited`; `507 repository_quota_exceeded|checkpoint_quota_exceeded|tombstone_quota_exceeded|quarantine_quota_exceeded|audit_quota_exceeded`; `503 bundle_busy|preview_authority_unavailable|preview_timeout`; or client transport timeout | prepared/terminal endpoint result, one coherent intent, zero/one preview and proposal | late pure worker cannot persist; it continues under a renewed capacity lease until actual exit | late pure error cannot persist and releases only after exit | same key/body only on transport timeout/429/503; intent recovery precedes fresh checks | stop heartbeat and release only when actual worker exits; process death is reclaimed by heartbeat expiry | `ASYNC-02`, `SERVICE-01`, `RESOURCE-03` |
| Supersede | Lineage generation; 30 s client wait; preview caller waits at most 60 s | create sequence plus final locked current-lineage/one-successor recheck | `201` successor read model; `409 stale_read_model|invalid_state|snapshot_changed|snapshot_unstable|idempotency_conflict|indeterminate_outcome`; `429 create_rate_limited|preview_concurrency_limited`; `507 repository_quota_exceeded|checkpoint_quota_exceeded|tombstone_quota_exceeded|quarantine_quota_exceeded|audit_quota_exceeded`; `503 bundle_busy|preview_authority_unavailable|preview_timeout|lock_unavailable|storage_corrupt`; or client transport timeout | zero/one successor, coherent service intent, terminal/indeterminate result | late pure worker remains capacity-accounted and cannot publish | late pure error releases only after actual exit | same key/fingerprint before stale/current-lineage evaluation | stop heartbeat/release on actual exit; clean only unreferenced loser stage | `ASYNC-03`, `SERVICE-02`, `RESOURCE-03` |
| Withdrawal | Control generation; 15 s absolute | endpoint prepared → service intent binding accepted token/exact row → withdrawal/outbox/result → committed | `200` withdrawn read model; `409 stale_read_model|invalid_state|idempotency_conflict|indeterminate_outcome`; `503 lock_unavailable|storage_corrupt`; or client transport timeout | coherent intent + prepared/terminal result + zero/one withdrawal | discard and canonical refetch | keep prior UI until refetch | same key reconciles before stale-token evaluation | settle waiter and dialog; preserve intent authority | `ASYNC-04`, `SERVICE-05..07` |
| Keyed decision | Control generation; 15 s absolute | endpoint prepared → service intent binding accepted token/exact row → decision/outbox/result → committed | `200` decision/tokens; `409 decision_conflict|stale_read_model|indeterminate_outcome`; `422 invalid_request`; `429 rate_limited`; `503 lock_unavailable|storage_corrupt`; or client transport timeout | coherent intent + prepared/terminal result + zero/one decision | discard UI response; canonical refetch | preserve explicit state until refetch | same key/fingerprint reconciles before stale-token evaluation | settle one waiter; preserve intent authority | `ASYNC-05`, `SERVICE-03`, `SERVICE-06..07` |
| Keyless decision | Control generation; 15 s | internal one-shot → decision | `200` decision/tokens; `409 decision_conflict|stale_read_model`; `422 invalid_request`; `429 rate_limited`; `503 lock_unavailable|storage_corrupt`; or ambiguous client transport timeout | internal result + zero/one decision | discard; refetch | show outcome unknown and refetch | never blind retry; new intent after CAS refetch | clear editor only after canonical result | `ASYNC-06` browser `tests/test_proposal_async.py::test_async_06_keyless_decision_ambiguous` |
| Apply before commit decision | Transaction waiter; 30 s client wait, server recovery owner | endpoint/WAL prepared or rollback | `202 transaction_in_progress`; `409 apply_rejected|proposal_conflict|stale_read_model|idempotency_conflict`; `422 validation_failed`; `500 contract_parity_failure`; `503 bundle_busy|transaction_recovering|lock_unavailable|storage_corrupt`; or client transport timeout | endpoint prepared/terminal; WAL absent/prepared/rolling_back | refetch; never infer applied | refetch; error cannot erase later canonical result | same key/body only | waiter may depart; server recovery continues | `ASYNC-07` fault `tests/test_proposal_async.py::test_async_07_apply_before_commit_decision` |
| Apply timeout before intent admission | Caller and worker share one repository-lock admission fence | private validation/staging only; no durable intent/WAL/endpoint | `503 operation_timeout` | fence is cancelled and recognized private files are removed; no transaction authority | worker can only observe cancellation and clean up; it cannot publish authority | no late mutation/result | a later same-key call starts fresh | close/join worker normally | barrier proof `tests/test_proposal_async.py::test_pre_authority_timeout_fence_cancels_without_late_authority` |
| Apply timeout after intent admission wins | Repository lock atomically publishes verified intent and marks admission before caller cancellation can win | durable intent; later history/WAL/endpoint recovery | `503 transaction_recovering` until progress is observable | exactly one transaction lineage | late worker/retry completes only that lineage | no second admission | same-key retry enters the complete lock envelope and recovers | recovery owns completion | barrier/racing retry `tests/test_proposal_async.py::test_pre_authority_timeout_admission_wins_same_key_retry_has_one_transaction` |
| Apply after commit decision | Server transaction/recovery; client 30 s wait | forward publish/finalize | `202 transaction_in_progress`, terminal `200 applied`, `409 indeterminate_outcome`, `503 storage_corrupt`, or client transport timeout | forward WAL, terminal result/audit when complete | refetch; success only from canonical terminal result | late transport error cannot revert success | same key/body; reconnect canonical read | no client cancellation of server | `ASYNC-08` fault/browser `tests/test_proposal_async.py::test_async_08_apply_after_commit_decision` |
| Undo before/after decision | Undo transaction/recovery; 30 s client wait | restore prepare/publish/finalize | `202 transaction_in_progress`, `200 undone`, `409 undo_conflicted|indeterminate_outcome`, `410 undo_expired`, `503 storage_corrupt`, or client transport timeout | discriminated undo WAL/result/audit/history links | refetch terminal undo | preserve prior apply audit | same key/body | same waiter/recovery rules | backend `ASYNC-09` direct fault `tests/test_proposal_undo.py::test_async_09_undo_mid_publish_recovers_exactly`; public transport/browser proof remains planned |
| Reconnect while transaction runs | Connection epoch; 10 s read | server transaction independent | `200` read model with closed `transaction_attempts`; `503 storage_corrupt|preview_authority_unavailable`; or client transport timeout | unchanged nonterminal WAL | current generation adopts only canonical projection | transport error leaves controls disabled | reconnect SSE then canonical read | dedupe waiters by transaction id | `ASYNC-10` browser `tests/test_proposal_browser.py::test_async_10_reconnect_transaction_progress` |
| SSE/poll/outbox | Event cursor; connection lifecycle | append exact payload stored in receipt; never reconstruct current state | SSE frame or `200` poll page; gap/resync frame; transport disconnect | pending/appended receipt bound to proposal revision, mutation intent/result | current owner invalidates/refetches | gaps force resync | at-least-once, cursor+outbox dedupe | close old source/timer | `ASYNC-11`, `OUTBOX-01..04` |

For the transport-neutral Bundle 3 boundary, list/show own one 10-second absolute read future, decision/withdraw own one 15-second absolute pre-intent deadline, and preview capture/stage/validation has one 60-second caller-wait deadline. That deadline aborts only the caller's wait; Python thread work is not aborted. A timed-out or cancelled preview caller does not release its active-worker lease: while the actual pure worker remains alive, a repository-owned heartbeat renews the lease before expiry, including after caller timeout. The future done callback stops heartbeat and releases that exact lease only after late success or late error. Thus repeated hung calls still consume at most two global workers and eight queued leases; process death stops heartbeat and expiry safely reclaims capacity. Timeout before intent admission persists nothing. Once an intent is admitted, repository recovery—not the departed caller—owns all later phases; late pure preview/read completion cannot publish repository authority. Decision/withdraw timeout observation never waits for the repository lock: a proved absent intent returns `operation_timeout`, busy/admitted authority returns `service_mutation_in_progress`, and a keyless decision returns ambiguous `indeterminate_outcome` requiring canonical refetch.
| Expiry crosses nonterminal WAL | Server recovery clock | transaction phase controls | `202 transaction_in_progress`; eventual `200 applied|undone`; `409 apply_rejected|undo_conflicted|indeterminate_outcome`; `410 undo_expired`; or `503 storage_corrupt`; expiry alone emits no competing response | exact expiry + WAL | forward transaction finishes after decision | pre-decision rollback terminal then revision projects expired | same key reconciles | no pruning until terminal | `ASYNC-12` fake-clock `tests/test_proposal_async.py::test_async_12_expiry_during_transaction` |
| A→B→A navigation | Navigation epoch | any dispatched server mutation remains independent | newest route may render only exact `200` canonical read; old responses are discarded | server canonical unaffected | old A discarded | old error discarded | new A canonical fetch | abort old fetch/backoff | `ASYNC-13` browser `tests/test_proposal_browser.py::test_async_13_a_b_a_epoch` |
| BFCache restore | pageshow epoch | none locally | `200` canonical resync required before controls; `409 cursor_expired|stale_read_model`; `503 storage_corrupt|preview_authority_unavailable`; or client transport timeout | server canonical | queued latest resync alone commits | error keeps controls disabled | canonical read before SSE reconnect | clear drafts/timers/sources | `ASYNC-14` browser `tests/test_proposal_browser.py::test_async_14_bfcache_resync` |
| Pagehide | Page lifecycle owner | dispatched server mutations continue | no departed-page public result is consumed | server authority unchanged | never touches departed DOM | never touches departed DOM | next page refetches | abort local waits only | `ASYNC-15` browser `tests/test_proposal_browser.py::test_async_15_pagehide_transaction_fence` |
| Apply enablement invalidation | Current read tuple owner | none | control disables synchronously; only a later `200` canonical read may enable | none | only exact tuple re-enables | any error remains disabled | fresh canonical read | close confirmation if tuple changes | `ASYNC-16` browser `tests/test_proposal_browser.py::test_async_16_apply_enable_tuple` |

### 12.1 Transport-neutral service async lifecycle

This matrix governs the Foundation Bundle 3 service independently of its registered experimental HTTP/CLI adapters and future Studio adapter. “Return” means the transport-neutral success/error envelope; registered adapters map it as §10 requires.

| Service operation | Deadline/abort owner | Durable sequence | Ambiguous/crash behavior | Retry/restart behavior | Resource cleanup | Required proof |
|---|---|---|---|---|---|---|
| Create | 60 s preview owner after bounded rate/worker admission | prepared endpoint → staged preview → prepared service intent → promoted preview → proposal → stored-payload outbox → terminal result → committed intent | no coherent intent beside prepared endpoint becomes indeterminate; coherent intent rolls exact rows forward | same key/fingerprint resumes before current-state checks on any worker/restart | release worker/queue slot; preserve referenced stage; startup removes only verified orphan/abandoned stage | `SERVICE-01`, `SERVICE-07`, `SERVICE-09` |
| Supersede | Same preview owner plus bounded lineage lock | create sequence, with final locked lineage/current-revision/uniqueness recheck | speculative loser cannot append a successor; ambiguity reconciles by key/intent | one successor only across keys/processes/restart | discard only unreferenced loser stage after authority scan | `SERVICE-02`, `SERVICE-08`, `IDEM-10` |
| Keyed decision | 15 s service owner | prepared endpoint → prepared intent binding accepted token/exact decision → decision → stored-payload outbox → terminal → committed | lost response never duplicates row; prepared-without-intent is indeterminate | reconcile same key/fingerprint before stale-token evaluation; changed non-token intent conflicts | settle waiter; no cache authority | `SERVICE-03`, `SERVICE-06`, `IDEM-11` |
| Keyless decision | 15 s service owner | same sequence under private one-shot identity | client treats transport loss as unknown | restart recovers intent; client refetches and creates a new CAS intent rather than blind retry | internal result 24 h; intent/audit binding remains | `SERVICE-04`, `IDEM-12` |
| Withdrawal | 15 s service owner | prepared endpoint → prepared intent binding accepted token/exact withdrawal → withdrawal → stored-payload outbox → terminal → committed | same as keyed decision | same-key reconciliation precedes stale-token/lifecycle evaluation | settle waiter; preserve exact intent until safe prune | `SERVICE-05`, `SERVICE-06` |
| List continuation | 10 s read owner | no mutation; pin signed high waters and reconstruct immutable projection | cache loss, worker change, or restart is irrelevant | matching retained facts continue; changed/pruned facts return `cursor_expired` | release pinned snapshot after page; no retention pin | `CURSOR-01..04` |
| Outbox reconciliation | Repository recovery owner; batches 1..100 | read pending receipt → validate stored payload/bindings → append/observe by outbox id → mark appended | append-before-mark may duplicate notification only | every worker/restart redelivers stored payload, never reconstructed state | bounded batch; attempts persist; receipt remains pending on failure | `OUTBOX-01..04` |
| Startup recovery | Repository startup owner; completes before active gate opens | validate service WAL, previews, aggregates, results, tombstones, outbox; roll forward or classify indeterminate | no proposal read/mutation is served from a partial recovery | deterministic repeat until committed/indeterminate; 100 failed attempts requires repair | preserve referenced evidence; clean verified orphans only | `SERVICE-07`, `RET-SVC-01..03` |

Browser in-memory cache keys include the complete apply-enable tuple from §7.1.
No stale-while-revalidate is permitted for decision controls, conflict state, validation freshness, or apply enablement.
Negative cache and transport failure never fabricate absence.
Concurrent pagination is snapshot-isolated by cursor high-water; a new initial read is required after mutation.
Proposal payloads, previews, extensions, and provenance MUST NOT be stored in Cache Storage, IndexedDB, localStorage, sessionStorage, service-worker caches, browser history state, URLs, query strings, fragments, referrers, analytics/telemetry, crash reports, or client logs.
Only non-sensitive ids, digests, cursor, layout preference, and event high-water may remain in memory for the current document; pagehide clears proposal body bytes.

## 13. Security and governance boundary

| Boundary | Normative rule | Proof |
|---|---|---|
| Operation channel | Only the five v1 data operations are admitted; no command or code execution | `SEC-01` schema fuzz |
| Containment | Canonical ids only; pinned root/session dirfds; no-follow opens; pre/post identity checks; visible source/control symlinks fail capture closed | `SEC-02` path-swap faults; `SNAP-04` symlink matrix |
| TOCTOU | Starvation-safe shared capture/exclusive writer/recovery ownership; complete recovery lock envelope; descriptor-relative Linux exchange/rename; exact before, staged, source, destination, and resulting identity checks immediately around publication. Same-byte/different-inode is hostile substitution, not equality | `SEC-03` race/exchange injection; `SNAP-05..06` concurrency |
| Resource use | §4 caps, bounded DAG traversal, bounded diff/validation, 15-second lock/deadline limits, retryable `bundle_busy` | `SEC-04` exhaustion; `SNAP-05` timeout |
| CSRF/exposure | Studio token is CSRF capability only; private proposal token is additionally required for mutation; mutation is loopback-only; public/tunnel editable activation fails; no-edit precedes body/idempotency | `SEC-05` HTTP/exposure/proxy matrix |
| Origin/target/method | Exact served-origin tuple, pre-parse target cap, strict UTF-8/query grammar, and reserved-prefix all-method dispatch fail closed | `tests/test_proposal_http.py` origin/target/method matrix |
| Secrets/logging | Never log token, idempotency key, proposal/extension/provenance bodies, staged bytes, or rationale; exclude URLs/referrers/telemetry/crash reports | `SEC-06` log/telemetry capture |
| Active code | Proposal staging and apply never invoke viewer plugins or active code; existing double-consent boundary remains separate | `SEC-07` plugin fixture |
| Frozen validation | Validation consumes only pinned captured bytes/inventories/warnings/policy; lexical escapes produce findings without host lookup or live-filesystem reads | `SNAP-02..03` filesystem-denial fixture |
| Passive constraints in proposal mode | This proposal backend does not use `studio.constraints` as validation, authorization, or mandatory-review policy. The direct Studio write funnel separately enforces [path/group limits](/reference/config_yaml.md#constraints). | `SEC-08` bypass compatibility |
| Direct compatibility | Existing direct writer commands, bytes, and public behavior remain unchanged; known okf-loom writers participate internally in exclusive bundle ownership, while external editors remain covered by double scan | `SEC-09` regression suite; `SNAP-05..06` |
| Output safety | All authored text is escaped; diff is text, never injected HTML; CSP remains current | `SEC-10` browser injection |

The server is LLM-free and harness-agnostic.
Proposal provenance may identify an external workflow but cannot confer authorization or automatic acceptance.
Author and actor labels are unverified display/audit labels, MUST be escaped, and MUST NOT be called authenticated identities.

## 14. Studio UX and accessibility contract

The Studio adds a proposal list and dedicated review workspace only when capability discovery says active.
The list exposes state, summary, author, age, affected count, decision counts, conflicts, validation freshness, expiry, and apply/undo outcome.
Loading uses a labelled busy region; empty says no proposals and explains direct mode remains available; errors persist with Retry; expired, superseded, withdrawn, applied, and conflicted states are textual and non-color-only.

At viewport widths of at least 900 CSS px the default diff is side-by-side with synchronized hunk navigation.
Inline mode is always available.
Below 900 px the workspace forces one-column inline diff, wraps long lines, permits local code-region scrolling only where unavoidable, and has no horizontal page overflow.
Before and after labels remain programmatically associated in every layout.
The operation tree shows explicit decision separately from derived blocked/conflicted/invalid state and lists blocking paths.

No-JavaScript output renders authored bundle content and one honest notice: “Proposal review requires JavaScript.”
It MUST NOT include proposal summaries, rationale, before/after bytes, decisions, conflict data, apply/undo controls, or claims of review state.

| Command/state | Key/control | Focus and default behavior | Proof |
|---|---|---|---|
| Enter workspace | Skip link / proposal link | Focus review heading, then summary, issues, operations, diff, actions in DOM order | `KEY-01` browser+a11y |
| Next/previous operation | `Alt+ArrowDown` / `Alt+ArrowUp` | Move focus to operation heading; do nothing in editable controls | `KEY-02` |
| Accept focused op | `Alt+A` | Opens confirmation only when state change affects dependents; otherwise activates button | `KEY-03` |
| Reject focused op | `Alt+R` | Opens reason dialog when dependents exist; reason required there | `KEY-04` |
| Diff next/previous hunk | `n` / `p` | Works only when diff owns focus; editable/native controls retain keys | `KEY-05` |
| Apply | Button only; no global shortcut | Confirmation names op/concept counts; focus enters dialog | `KEY-06` |
| Escape in dialog | `Escape` | The owned topmost dialog handles Escape and restores its invoking control; a native editable retains text-editing commands but Escape belongs to the enclosing owned dialog unless an inner native widget has an open popup; dispatched apply/undo is not cancelled | `KEY-07` |
| Decision success | n/a | Restore same op control or heading; announce new explicit and derived state | `KEY-08` |
| Conflict/error | n/a | User-triggered failure focuses persistent issue summary; background refresh never steals focus | `KEY-09` |
| Reorder/removal | n/a | Preserve focus by proposal/op id; otherwise nearest item then panel heading | `KEY-10` |
| Native ownership | typing, copy, delete, browser shortcuts | Never intercept native text commands inside input, textarea, select, contenteditable, or modifier conflicts; Escape follows the explicit topmost-dialog rule above | `KEY-11` |
| Apply/undo success | terminal canonical refetch | Close confirmation; focus outcome heading, announce operation, concept count, and undo availability | `KEY-12` |
| Apply/undo terminal failure | terminal canonical refetch | Keep workspace; focus persistent issue summary and announce code plus recovery action | `KEY-13` |
| Apply/undo indeterminate | canonical indeterminate result | Focus non-dismissible recovery alert; disable mutation controls; announce that success/failure is unknown | `KEY-14` |
| Apply/undo in progress or late result | transaction id owner | Focus stays on status region or restored invoking control; polite announcement; late result never steals focus | `KEY-15` |

All controls have visible focus with at least a 2 CSS px indicator and 3:1 contrast.
Forced-colors mode uses system colors, borders, and text labels rather than background color alone.
Reduced-motion disables smooth scrolling, diff animation, and status transitions without delaying state.
A polite live region announces background counts and freshness changes; an assertive region is reserved for user-triggered apply/undo failure.
The persistent issue summary links to every blocked, conflicted, invalid, or stale operation.

## 15. Migration matrix

| Input/version | Read behavior | Write/apply behavior | Proof |
|---|---|---|---|
| Legacy suggestion valid | Project `legacy-suggestion` with `{legacy_id,source_generation}` | Cannot decide/apply; regeneration must repeat both identity fields in request and provenance | `MIG-01` fixture |
| Legacy suggestion malformed complete row | Storage diagnostic/quarantine | Never guess or migrate | `MIG-02` corruption |
| Legacy suggestion duplicate id | Distinct source-generation summaries; diagnostic | Regeneration requires exact generation identity | `MIG-03` |
| Protocol-v1 proposal | Full canonical behavior | Normal v1 contract | `MIG-04` E2E |
| Unknown future proposal schema | Preserve file bytes only during backup; service reports unsupported | No projection, decision, or apply | `MIG-05` fail-closed |
| Existing `/__apply` client | Current direct behavior | Unchanged | `MIG-06` regression |
| Existing direct CLI/mutator | Current direct behavior | Unchanged | `MIG-07` regression |
| Capability disabled | No proposal surface | `capability_unavailable`; direct mode works | `MIG-08` integration |
| Session root moved/configured | Open only configured root; recover there | No implicit migration from old root | `MIG-09` containment |
| Pre-release apply/undo transaction schema | No persisted production apply/undo transaction data exist | Adopt intent-v1 and required WAL `transaction_intent_digest` directly; no migration, backfill, compatibility reader, or fabricated intent/history | `MIG-10` release fixture |

## 16. Apply, rollback, and idempotency requirement matrices

| Apply/rollback case | Required outcome | Proof |
|---|---|---|
| Empty accepted set | `409 invalid_state`; no WAL | `APPLY-01` unit/HTTP |
| Accepted set not dependency-closed | `409 proposal_conflict`; no WAL | `APPLY-02` |
| Final staged validation fails | `422 validation_failed`; no WAL | `APPLY-03` full-bundle fixture |
| Validation context differs from reviewed authority | `409 proposal_conflict` with `validation_stale`; no WAL | `APPLY-03A` frozen-context fixture |
| Canonical target identity differs before apply, even with equal bytes | `409 proposal_conflict` with `target_identity_changed`; no WAL or publication | `APPLY-03B` same-byte inode fixture |
| Staged bytes differ from reviewed bytes | `500 contract_parity_failure`; no publish | `APPLY-04` parity fault |
| All preflight succeeds | One intent/history/WAL-bound transaction and group; a one-target transaction uses the guarded direct `commit_decided → content_published` transition, a multi-target transaction durably publishes at least one proper-prefix `publishing` state, and all targets and exact candidate metadata publish | `APPLY-05` E2E |
| Pre-WAL failure, including intent/history publication | No canonical mutation; only provably orphaned private authority is cleaned | `APPLY-06` fault injection |
| Failure before durable commit decision | Restore/stay at all before bytes; never report partial success | `APPLY-07` crash matrix |
| Failure at/after durable commit decision | Roll forward every target and metadata or become typed indeterminate; preserve the single-target direct-transition guard and the multi-target proper-prefix requirement during recovery; never roll back | `APPLY-07A` crash matrix |
| Foreign identity or exchange race during recovery, including same bytes at another inode | `indeterminate`; preserve evidence and block affected serving | `APPLY-08` cross-process fault |
| Undo all parent resulting identities exactly current | All targets restored and linked | `APPLY-09` E2E undo |
| Undo one identity stale, ABA-changed, missing, or pruned | `conflicted`/`expired`/`indeterminate` as authority permits; restore none | `APPLY-10` |

| Idempotency case | Required result | Proof |
|---|---|---|
| Same key/body before WAL | Zero authority when the caller's repository-lock cancellation fence wins; exactly one recoverable transaction when atomic intent admission wins | `tests/test_proposal_async.py::test_pre_authority_timeout_fence_cancels_without_late_authority`; `::test_pre_authority_timeout_admission_wins_same_key_retry_has_one_transaction`; `IDEM-01` concurrent process |
| Same key/body after any crash point | Resume/replay same transaction | `IDEM-02` all crash points |
| Same key/different body | `idempotency_conflict`; no mutation | `IDEM-03` HTTP |
| Response lost after commit | Exact result with `replayed:true` | `IDEM-04` network fault |
| Result compacted but certain tombstone retained | Stable terminal summary, no second apply | `IDEM-05` retention |
| Authority corrupt/pruned and outcome uncertain | `indeterminate_outcome`; no mutation | `IDEM-06` corruption |
| Concurrent different keys for same revision | Proposal/target locks permit one winner; loser refetches applied state | `IDEM-07` cross-process |
| Outbox failure after content commit | Recovery observes or appends by outbox id; delivery is at least once and may duplicate | `IDEM-08` outbox fault |
| Create/supersede crash or lost response | Endpoint result + proposal/preview reconcile to one revision or indeterminate | `IDEM-09..10` |
| Keyed/keyless decision crash or lost response | Keyed replays once; keyless refetches before new CAS intent | `IDEM-11..12` |

`APPLY-03A` has one exact wire contract: HTTP `409`, top-level error code
`proposal_conflict`, and `details` containing exactly one §5.5 proposal-level
conflict with `code:"validation_stale"`, null `op_id`/`concept`, `expected` equal
to the reviewed `validation_context_digest`, and `actual` equal to the locked
current digest. It writes no intent, history, WAL, endpoint result, or content.

## 17. Normative QUALITY_PROOF_MATRIX

Every P0 row below requires passing executable evidence against final serialized code and docs.
“Contract” means schema fixtures shared by Python, HTTP, CLI, and browser where applicable.

| QUALITY_RISK_CLASSIFICATION | Governing sections | Required test layers and named proof set | Priority |
|---|---|---|---|
| `state-ownership` | §§7, 12 | unit state model, cross-process CAS, browser ownership; `STATE-01..19`, `LIFE-01..10` | P0 |
| `api-protocol` | §§5, 10, 11 | duplicate-key/schema fuzz, HTTP/CLI parity; `HTTP-01..07`, `ERR-01..14` | P0 |
| `async-lifecycle` | §12 | fake-clock browser, abort/deadline, navigation/BFCache; `ASYNC-01..16` | P0 |
| `security-boundary` | §§4, 6, 13 | path/symlink race, auth precedence, injection, resource exhaustion, no-host validation; `SEC-01..11`, `SNAP-03..06` | P0 |
| `resource-bounds` | §§4, 7.3, 9, 12.1 | pre-parse caps, final-lock limits, preview admission, bounded list projection, explicit composition shutdown; `RESOURCE-01..05` | P0 |
| `contract-runtime-parity` | §§5–6, 10–11 | shared fixtures across staging/apply/diff/validation/API/CLI; `APPLY-04`, `PARITY-01..05`, `SNAP-01..02` | P0 |
| `fail-closed-validation` | §§4–6, 9–10 | nullable-finding/schema fuzz, frozen context, corrupt storage, malformed DAG/path; `ERR-05..14`, `STORE-01..14`, `SNAP-01..06` | P0 |
| `idempotency-retry` | §§5.6, 8, 16 | cross-process and every fault point; `CRASH-01..18`, `IDEM-01..13` | P0 |
| `service-mutation-recovery` | §§5.6A, 9.1, 12.1 | transport-neutral fault/restart/cross-process proof; `SERVICE-01..10`, `RET-SVC-01..03` | P0 |
| `outbox-immutable-payload` | §§5.7, 12.1 | stored-payload/digest/binding and append-before-mark faults; `OUTBOX-01..04` | P0 |
| `pagination-concurrency` | §§10, 12 | concurrent append/page walk, cursor tamper/expiry/prune; `PAGE-01..06` | P0 |
| `cache-invalidation` | §§6, 12 | validation-context drift, SSE/poll gaps, stale token, late result, A→B→A, BFCache; `ASYNC-01..16`, `CACHE-01..05`, `SNAP-02` | P0 |
| `migration-safety` | §§9, 15 | legacy/corrupt/future-version/direct regression and pre-release transaction-schema adoption; `MIG-01..11` | P0 |
| `keyboard-focus` | §14 | real-browser keyboard, accessibility tree, forced colors/reduced motion/no-JS; `KEY-01..15`, `A11Y-01..06` | P0 |

### 17.1 Required planned proof catalogue

Every row in this catalogue is a release obligation unless an external evidence
report records the exact node, revision, platform, and passing result. A test
path's presence is not proof that it passed, and this specification edit does
not claim to have executed the proposal suite. Existing experimental code and
tests may provide partial evidence, but no range label is a blanket PASS.
Transport-neutral apply/undo backend proofs and public HTTP/CLI adapter tests
execute. The Studio decision UI, browser serialization, and public end-to-end
proof remain unavailable. The only Playwright/UI evidence is
`tests/test_proposal_inspection_browser.py` for the inspection-only panel; it
is not claimed as evidence for any catalogue row below.
Paths are required intended test locations; implementation may split a row only if the named node remains as the aggregate release gate.

| State proof | Obligation | Layer and intended path/node |
|---|---|---|
| `STATE-01` | Concept bytes own content revision, while complete canonical identity separately detects same-byte inode/parent substitution | unit/cross-process `tests/test_proposal_state.py::test_state_01_concept_authority` |
| `STATE-02` | Proposal aggregate/checkpoint owns immutable revision | restart `tests/test_proposal_repository.py::test_state_02_proposal_authority` |
| `STATE-03` | Latest decision row and CAS own explicit decision | cross-process `tests/test_proposal_decisions.py::test_state_03_decision_cas` |
| `STATE-04` | Blockers/conflicts/local validity are pure projections | unit `tests/test_proposal_projectors.py::test_state_04_derived_only` |
| `STATE-05` | Lifecycle precedence is deterministic | unit `tests/test_proposal_projectors.py::test_state_05_lifecycle_precedence` |
| `STATE-06` | Intent owns exact finalization, WAL owns phase and before/resulting identities, and result owns replay; endpoint preparation follows prepared WAL | fault `tests/test_proposal_transactions.py::test_state_06_wal_result_authority` |
| `STATE-07` | Existing event cursor remains notification authority | HTTP `tests/test_proposal_events.py::test_state_07_event_cursor` |
| `STATE-08` | Browser route/generation fences DOM ownership | browser `tests/test_proposal_browser.py::test_state_08_route_generation` |
| `STATE-09` | Immutable history plus terminal audit/WAL lifecycle authority, retention, and exact parent resulting-identity equality own undo availability and reject ABA | fault `tests/test_proposal_undo.py::test_state_09_undo_authority` |
| `STATE-10` | Endpoint result then tombstone owns replay | fault `tests/test_proposal_idempotency.py::test_state_10_result_tombstone` |
| `STATE-11` | Transaction aggregate is identity-bound audit, not replay authority; terminal startup validates every associated authority | restart `tests/test_proposal_audit.py::test_state_11_transaction_audit` |
| `STATE-12` | Signed high-water cursor owns immutable pagination snapshot | concurrency `tests/test_proposal_pagination.py::test_state_12_snapshot_authority` |
| `STATE-13` | Full browser cache key fences stale state | browser `tests/test_proposal_browser.py::test_state_13_cache_authority` |
| `STATE-14` | Exact persisted instant plus clock owns expiry | fake-clock `tests/test_proposal_retention.py::test_state_14_expiry` |
| `STATE-15` | Three runtime ids or validation-context authority invalidate freshness | parity `tests/test_proposal_preview.py::test_state_15_runtime_and_validation_context` |
| `STATE-16` | Complete tuple alone enables apply | browser `tests/test_proposal_browser.py::test_state_16_apply_tuple` |
| `STATE-17` | Receipt/event observation implements at-least-once outbox | fault `tests/test_proposal_events.py::test_state_17_outbox_authority` |
| `STATE-18` | Waiters are non-authoritative and restart-safe | async `tests/test_proposal_transactions.py::test_state_18_waiters` |
| `STATE-19` | Nonterminal WAL maps to one closed transaction-progress projection | projector `tests/test_proposal_projectors.py::test_state_19_transaction_progress` |

| Lifecycle proof | Obligation | Layer and intended path/node |
|---|---|---|
| `LIFE-01` | Valid create enters in_review after preview durability | HTTP/restart `tests/test_proposal_lifecycle.py::test_life_01_create` |
| `LIFE-02` | Complete valid decisions derive ready | projector `tests/test_proposal_lifecycle.py::test_life_02_ready` |
| `LIFE-03` | Canonical change derives conflicted without decision mutation | concurrency `tests/test_proposal_lifecycle.py::test_life_03_conflict` |
| `LIFE-04` | Exact restoration removes derived conflict only | unit `tests/test_proposal_lifecycle.py::test_life_04_restore` |
| `LIFE-05` | Only live review states may supersede; no transfer | HTTP `tests/test_proposal_lifecycle.py::test_life_05_supersede` |
| `LIFE-06` | Withdrawal is one proposal aggregate row under CAS | cross-process `tests/test_proposal_lifecycle.py::test_life_06_withdraw` |
| `LIFE-07` | Committed apply derives applied | E2E `tests/test_proposal_e2e.py::test_life_07_apply` |
| `LIFE-08` | Exact boundary derives expired and blocks mutation | fake-clock `tests/test_proposal_lifecycle.py::test_life_08_expiry` |
| `LIFE-09` | Every prohibited terminal transition returns invalid_state | table `tests/test_proposal_lifecycle.py::test_life_09_illegal_transitions` |
| `LIFE-10` | Audit pruning leaves non-reusable audit-expired tombstone | retention `tests/test_proposal_lifecycle.py::test_life_10_audit_expiry` |

| Dependency proof | Obligation | Layer and intended path/node |
|---|---|---|
| `DEP-01` | Invalid local operation wins effective-state precedence | projector `tests/test_proposal_dependencies.py::test_dep_01_invalid_precedence` |
| `DEP-02` | Conflict wins over explicit decision/blocker | projector `tests/test_proposal_dependencies.py::test_dep_02_conflict_precedence` |
| `DEP-03` | Rejection remains explicit when otherwise valid | projector `tests/test_proposal_dependencies.py::test_dep_03_rejected` |
| `DEP-04` | Transitive prerequisite state derives sorted blockers | graph/unit `tests/test_proposal_dependencies.py::test_dep_04_transitive_blocking` |
| `DEP-05` | Only accepted, valid, unconflicted, unblocked op enters apply set | unit `tests/test_proposal_dependencies.py::test_dep_05_apply_membership` |
| `DEP-06` | Absent decision remains undecided | unit `tests/test_proposal_dependencies.py::test_dep_06_undecided` |
| `DEP-07` | The exact effective dependency-closed set, excluding explicitly accepted but blocked/conflicted/invalid operations, is byte-identical in restaging, intent, WAL, audit, and target publication | backend integration `tests/test_proposal_apply.py::test_dep_07_effective_accepted_authority` |

| Conflict proof | Obligation | Layer and intended path/node |
|---|---|---|
| `CONFLICT-01` | Full snapshot mismatch yields deterministic concept details | integration `tests/test_proposal_conflicts.py::test_conflict_01_snapshot` |
| `CONFLICT-02` | Accepted read revision mismatch is op/concept-scoped | unit `tests/test_proposal_conflicts.py::test_conflict_02_read_revision` |
| `CONFLICT-03` | Accepted write revision mismatch is op/concept-scoped | unit `tests/test_proposal_conflicts.py::test_conflict_03_write_revision` |
| `CONFLICT-04` | Missing/type/complete-identity/capability/config/validation-context mismatch maps exact code | table `tests/test_proposal_conflicts.py::test_conflict_04_target_environment` |
| `CONFLICT-05` | Stale decision/page/proposal token cannot mutate | HTTP `tests/test_proposal_conflicts.py::test_conflict_05_tokens` |
| `CONFLICT-06` | Current complete authority alone reaches preflight | integration `tests/test_proposal_conflicts.py::test_conflict_06_clean_preflight` |

| WAL proof | Obligation | Layer and intended path/node |
|---|---|---|
| `WAL-01` | No WAL means no publish authority even if an ordered pre-WAL intent/history pair exists | fault `tests/test_proposal_wal.py::test_wal_01_none` |
| `WAL-02` | Prepared requires exact intent/history/identity linkage, precedes prepared endpoint authority, forbids canonical publication, and permits rollback only; crash after WAL before endpoint is recoverable | fault `tests/test_proposal_wal.py::test_wal_02_prepared` |
| `WAL-03` | Durable commit_decided forces forward recovery from verified staged identity; one target may advance directly only after descriptor-relative exchange/rename, both-end exact identity verification, parent fsync, and resulting-identity WAL capture, while multiple targets must advance first to `publishing` | fault `tests/test_proposal_wal.py::test_wal_03_commit_decided` |
| `WAL-04` | `publishing` exists only for multiple targets and every durable bitmap is a non-empty canonical proper prefix; multi-target `commit_decided → content_published` and one-target `publishing` are rejected | property/fault `tests/test_proposal_wal.py::test_wal_04_publishing` |
| `WAL-05` | `content_published` means every exact resulting identity—not digest alone—is canonical and every source/destination and parent is verified/fsynced; predecessor cardinality rules remain enforced | fault `tests/test_proposal_wal.py::test_wal_05_content_published` |
| `WAL-06` | Finalizing verifies intent→immutable-history→WAL without replacing history, keeps candidate checksums as placeholders in the intent digest, substitutes exact WAL resulting identities and the verified intent digest into audit, computes and validates the closed audit checksum, then resolves audit-bound endpoint/outbox values and containing checksums in topological order; missing/corrupt/cyclic authority fails closed without fabrication | restart/corruption `tests/test_proposal_wal.py::test_wal_06_finalizing` |
| `WAL-07` | Committed replay survives WAL retirement through durable links | restart `tests/test_proposal_wal.py::test_wal_07_committed_retirement` |
| `WAL-08` | Pre-decision rollback uses reverse suffix and restores all | fault `tests/test_proposal_wal.py::test_wal_08_rollback` |
| `WAL-09` | Missing identity, same-byte/different-inode, exchange race, or contradictory terminal authority becomes marker/tombstone indeterminate and blocks serving | corruption `tests/test_proposal_wal.py::test_wal_09_indeterminate` |
| `WAL-10` | Multi-target pre-decision rollback durably records every reverse prefix, including an all-true `rolling_back` bitmap, before `rolled_back` or rollback finalization; every skipped prefix is rejected | process-crash fault `tests/test_proposal_crash.py::test_wal_10_multi_target_reverse_prefix` |

| Retention proof | Obligation | Layer and intended path/node |
|---|---|---|
| `RET-01` | Audit fields and links remain 180 days | fake-clock/restart `tests/test_proposal_retention.py::test_ret_01_audit_horizon` |
| `RET-02` | Preview survives 30 days and never prunes pre-terminal | fake-clock `tests/test_proposal_retention.py::test_ret_02_preview_horizon` |
| `RET-03` | Replay result compacts only to sufficient tombstone | fake-clock `tests/test_proposal_retention.py::test_ret_03_replay_horizon` |
| `RET-04` | Nonterminal/uncertain intent and WAL association never age-prunes or quota-evicts | fake-clock/fault `tests/test_proposal_retention.py::test_ret_04_wal_authority` |
| `RET-05` | History/backup undo remains restart-provable until exact expiry | fake-clock/restart `tests/test_proposal_retention.py::test_ret_05_history_undo` |
| `RET-06` | Event prune creates typed resync, not missed canonical state | HTTP `tests/test_proposal_retention.py::test_ret_06_event_gap` |

| Schema proof | Obligation | Layer and intended path/node |
|---|---|---|
| `SCHEMA-01` | Proposal/withdrawal closed immutable schemas | contract `tests/test_proposal_schemas.py::test_schema_01_proposal` |
| `SCHEMA-02` | All five operation variants and op-id ownership | property `tests/test_proposal_schemas.py::test_schema_02_operations` |
| `SCHEMA-03` | Snapshot/sole preview/diff/validation digest parity, nullable finding ownership, `line:0`, and bundle-first ordering | fixture `tests/test_proposal_schemas.py::test_schema_03_preview` |
| `SCHEMA-04` | Decision request/row/token/reason rules | cross-process `tests/test_proposal_schemas.py::test_schema_04_decision` |
| `SCHEMA-05` | Closed conflict codes/null ownership/sort | unit `tests/test_proposal_schemas.py::test_schema_05_conflict` |
| `SCHEMA-06` | Apply/undo intent candidates/placeholders and cardinality-sensitive WAL intent-digest/phase/bitmap/transition combinations are closed | property/fault `tests/test_proposal_schemas.py::test_schema_06_wal` |
| `SCHEMA-07` | Result/audit/intent/history/backup/undo/withdraw links complete | E2E `tests/test_proposal_schemas.py::test_schema_07_audit` |
| `SCHEMA-08` | Wrapper/checkpoint/sequence/generation closed | recovery `tests/test_proposal_schemas.py::test_schema_08_storage` |
| `SCHEMA-09` | Notification/read/page/runtime token schemas | HTTP/browser `tests/test_proposal_schemas.py::test_schema_09_wire` |
| `SCHEMA-10` | Legacy closed identity and projection | migration `tests/test_proposal_schemas.py::test_schema_10_legacy` |
| `SCHEMA-11` | Duplicate/unknown/type/depth/size UTF-8 corpus for every public family | fuzz `tests/test_proposal_schemas.py::test_schema_11_public_corpus` |

| Snapshot/validation proof | Obligation | Layer and intended path/node |
|---|---|---|
| `SNAP-01` | Bundle/reserved/config/capability findings alone permit null concept; concept findings require canonical id; `line:0` and null-safe sort are exact | property/fixture `tests/test_proposal_validation.py::test_snap_01_nullable_findings` |
| `SNAP-02` | Reserved bytes, validation-relevant non-concept inventory, scan-control bytes/manifests, warnings, and frozen policy all change `validation_context_digest`; parsed config semantics remain separately bound by `config_digest` | fixture/parity `tests/test_proposal_snapshot.py::test_snap_02_validation_context_digest` |
| `SNAP-03` | Frozen validation performs no live-filesystem or host lookup; lexical escapes yield deterministic captured-input findings | filesystem-denial `tests/test_proposal_validation.py::test_snap_03_no_live_filesystem` |
| `SNAP-04` | Every visible concept/reserved/validation-relevant non-concept/config/control symlink fails capture closed | filesystem matrix `tests/test_proposal_snapshot.py::test_snap_04_visible_symlinks` |
| `SNAP-05` | Shared captures and exclusive known writers obey global order, writer fairness, 15-second bound, retryable `bundle_busy`, and no unlocked fallback | cross-process/fake-clock `tests/test_proposal_snapshot.py::test_snap_05_shared_exclusive_lock` |
| `SNAP-06` | Uncoordinated direct edits are detected by exact double scan, retry once, then `snapshot_unstable` | race fault `tests/test_proposal_snapshot.py::test_snap_06_double_scan` |

| HTTP proof | Obligation | Layer and intended path/node |
|---|---|---|
| `HTTP-01` | Create/supersede/legacy request, 201 envelope, required key | HTTP `tests/test_proposal_http.py::test_http_01_create` |
| `HTTP-02` | List strict query, signed cursor, page token | HTTP `tests/test_proposal_http.py::test_http_02_list` |
| `HTTP-03` | Show returns closed read model/progress projection/no-store | HTTP `tests/test_proposal_http.py::test_http_03_show` |
| `HTTP-04` | Decision keyed/keyless CAS and exact success | HTTP `tests/test_proposal_http.py::test_http_04_decision` |
| `HTTP-05` | Withdrawal key/CAS/terminal transitions | HTTP `tests/test_proposal_http.py::test_http_05_withdraw` |
| `HTTP-06` | Apply 202/200/error matrix and replay precedence | HTTP `tests/test_proposal_http.py::test_http_06_apply` |
| `HTTP-07` | Undo 202/200/error matrix and history association | HTTP `tests/test_proposal_http.py::test_http_07_undo` |

| Error proof | Obligation | Layer and intended path/node |
|---|---|---|
| `ERR-01` | Invalid Host masks lower authorities | HTTP `tests/test_proposal_http.py::test_err_01_host` |
| `ERR-02` | Invalid CSRF capability masks lower authorities | HTTP `tests/test_proposal_http.py::test_err_02_token` |
| `ERR-03` | Origin failure precedence | HTTP `tests/test_proposal_http.py::test_err_03_origin` |
| `ERR-04` | Route/method/capability statuses exact | HTTP `tests/test_proposal_http.py::test_err_04_route` |
| `ERR-05` | No-edit rejects before locality/body/idempotency | HTTP `tests/test_proposal_http.py::test_err_05_no_edit` |
| `ERR-06` | Nonloopback mutation rejects before body/idempotency | HTTP `tests/test_proposal_http.py::test_err_06_loopback` |
| `ERR-07` | Raw body cap precedes parsing | HTTP `tests/test_proposal_http.py::test_err_07_body_cap` |
| `ERR-08` | Content type/JSON/duplicate-key failures exact | HTTP `tests/test_proposal_http.py::test_err_08_json` |
| `ERR-09` | Closed schema/path/dependency failures exact | HTTP `tests/test_proposal_http.py::test_err_09_schema` |
| `ERR-10` | Rate/quota/`bundle_busy`/other-lock/storage codes and retry flags exact | HTTP `tests/test_proposal_http.py::test_err_10_resource` |
| `ERR-11` | Idempotency conflict/indeterminate precedence exact | HTTP `tests/test_proposal_http.py::test_err_11_idempotency` |
| `ERR-12` | Missing/expired/terminal-state codes exact | HTTP `tests/test_proposal_http.py::test_err_12_resource_state` |
| `ERR-13` | CAS/snapshot/conflict/validation precedence exact | HTTP `tests/test_proposal_http.py::test_err_13_conflict` |
| `ERR-14` | Internal/parity errors stay bounded and secret-free | HTTP `tests/test_proposal_http.py::test_err_14_internal` |

The public-browser `ASYNC-01..16` obligations remain planned. The implemented
transport-specific committed-mutation/outbox-failure lifecycle is executable at
`tests/test_proposal_http.py::test_transport_async_committed_mutation_outbox_failure_retry`.

| Security proof | Obligation | Layer and intended path/node |
|---|---|---|
| `SEC-01` | Closed operations reject execution channels | schema fuzz `tests/test_proposal_security.py::test_sec_01_closed_ops` |
| `SEC-02` | Component openat/no-follow/owner/mode/link-count contract, including visible capture symlinks | filesystem `tests/test_proposal_security.py::test_sec_02_trusted_dirfds` |
| `SEC-03` | Parent/leaf/stage substitution, same-byte inode swap, exchange race, and concurrent recovery/direct-writer/capture attempts fail under descriptor-relative publication and the complete lock envelope | race fault `tests/test_proposal_security.py::test_sec_03_substitution_matrix` |
| `SEC-04` | Every resource/rate/quota bound returns typed failure | exhaustion `tests/test_proposal_security.py::test_sec_04_resource_caps` |
| `SEC-05` | CSRF is not auth; mutation loopback/no-edit/exposure precedence | HTTP `tests/test_proposal_security.py::test_sec_05_exposure_matrix` |
| `SEC-06` | Bodies absent from logs, URL, referrer, telemetry, crash reports | capture `tests/test_proposal_security.py::test_sec_06_secret_sinks` |
| `SEC-07` | Active plugins never run in preview/apply | integration `tests/test_proposal_security.py::test_sec_07_active_code_separation` |
| `SEC-08` | Constraints remain passive and bypass-neutral | regression `tests/test_proposal_security.py::test_sec_08_constraints_passive` |
| `SEC-09` | Direct clients remain byte/behavior compatible | regression `tests/test_proposal_direct_compat.py::test_sec_09_direct_mode` |
| `SEC-10` | Diff/authored strings escape under CSP | browser `tests/test_proposal_browser.py::test_sec_10_output_injection` |
| `SEC-11` | Faults injected immediately after exchange and after parent fsync detect source, destination, and parent substitution, preserve evidence, write exactly one blocking corruption marker plus indeterminate endpoint authority, and never claim hostile prevention | six-case syscall-seam fault `tests/test_proposal_transaction_security.py::test_sec_11_after_syscall_identity_seams` |

| Parity proof | Obligation | Layer and intended path/node |
|---|---|---|
| `PARITY-01` | Preview and transport-neutral apply coordinator serializer produce identical bytes; the registered public adapter preserves the same service contract | backend fixture `tests/test_proposal_parity.py::test_parity_01_bytes` |
| `PARITY-02` | Unified diff digest is identical across implemented service/CLI projections; browser parity remains planned | implemented-adapter fixture `tests/test_proposal_parity.py::test_parity_02_diff` |
| `PARITY-03` | Validator identity/context/findings/delta identical without live-filesystem access | fixture `tests/test_proposal_parity.py::test_parity_03_validation` |
| `PARITY-04` | The seven registered create/list/show/decide/withdraw/apply/undo HTTP adapters map domain envelopes/status exactly | contract `tests/test_proposal_parity.py::test_parity_04_http` |
| `PARITY-05` | Registered proposal CLI and HTTP JSON/exit semantics match for the seven operations | CLI `tests/test_proposal_parity.py::test_parity_05_cli` |

| Storage proof | Obligation | Layer and intended path/node |
|---|---|---|
| `STORE-01` | One repository owns every path/sequence | architecture `tests/test_proposal_repository.py::test_store_01_single_owner` |
| `STORE-02` | Atomic append and torn final line recovery | fault `tests/test_proposal_repository.py::test_store_02_torn_tail` |
| `STORE-03` | Complete corruption appends marker and blocks; no omission | corruption `tests/test_proposal_repository.py::test_store_03_corruption_marker` |
| `STORE-04` | Checkpoint high-water merge is immutable | concurrency `tests/test_proposal_repository.py::test_store_04_checkpoint_snapshot` |
| `STORE-05` | Rotation sequence and parent fsync survive crash | fault `tests/test_proposal_repository.py::test_store_05_rotation` |
| `STORE-06` | Pruning honors all authority/retention references | retention `tests/test_proposal_repository.py::test_store_06_pruning` |
| `STORE-07` | Dirfd remains pinned through directory substitution | filesystem `tests/test_proposal_repository.py::test_store_07_dir_substitution` |
| `STORE-08` | Restart recovers every nonterminal phase and validates/reconstructs-or-blocks every terminal intent/history/audit/result/activity/outbox chain before serving | restart `tests/test_proposal_repository.py::test_store_08_startup_recovery` |
| `STORE-09` | Preview has one artifact authority; no inline fallback | corruption `tests/test_proposal_repository.py::test_store_09_preview_authority` |
| `STORE-10` | Quarantine copy is non-authoritative; explicit repair required | operator repair `tests/test_proposal_repository.py::test_store_10_repair` |
| `STORE-11` | Every aggregate quota blocks safely without authority prune | quota `tests/test_proposal_repository.py::test_store_11_quotas` |
| `STORE-12` | Withdrawal/proposal and undo/transaction topology enforced | architecture `tests/test_proposal_repository.py::test_store_12_topology` |
| `STORE-13` | Intent→immutable-history→prepared-WAL→prepared-endpoint ordering, audit/WAL identity linkage, WAL retirement, restart association, missing aggregate refusal, retention, and prune | repository/restart `tests/test_proposal_repository.py::test_store_13_history_manifest_authority` |
| `STORE-14` | History schema rejects `state` and `resulting_identity`, remains byte-identical from prepare through commit/rollback/restart/undo, and has the same manifest digest in intent and every audit | schema/commit/rollback/undo parity `tests/test_proposal_repository.py::test_store_14_immutable_history_parity` |

| Pagination proof | Obligation | Layer and intended path/node |
|---|---|---|
| `PAGE-01` | Unique descending tuple gives stable order | unit `tests/test_proposal_pagination.py::test_page_01_order` |
| `PAGE-02` | Multi-page walk has no skip or duplicate | integration `tests/test_proposal_pagination.py::test_page_02_no_skip_duplicate` |
| `PAGE-03` | Concurrent append/bundle/validation-context/clock changes do not enter frozen pages | concurrency/fake-clock `tests/test_proposal_pagination.py::test_page_03_frozen_inputs` |
| `PAGE-04` | Cursor tamper/query mismatch fails closed | HTTP `tests/test_proposal_pagination.py::test_page_04_tamper` |
| `PAGE-05` | Cursor expiry requires fresh initial read | fake-clock `tests/test_proposal_pagination.py::test_page_05_expiry` |
| `PAGE-06` | Pruned/corrupt frozen authority returns resync/error, not omission | fault `tests/test_proposal_pagination.py::test_page_06_pruned_authority` |

| Cache proof | Obligation | Layer and intended path/node |
|---|---|---|
| `CACHE-01` | Known notification invalidates full tuple | browser `tests/test_proposal_browser.py::test_cache_01_notification` |
| `CACHE-02` | Unknown event/gap clears cache and refetches | browser `tests/test_proposal_browser.py::test_cache_02_gap` |
| `CACHE-03` | Late response cannot commit after route/generation change | browser `tests/test_proposal_browser.py::test_cache_03_late_response` |
| `CACHE-04` | Any token/runtime/validation-context/expiry mismatch disables controls | browser `tests/test_proposal_browser.py::test_cache_04_tuple_mismatch` |
| `CACHE-05` | no-store and no browser/service-worker persistence | browser/network `tests/test_proposal_browser.py::test_cache_05_no_persistence` |

| Idempotency proof | Obligation | Layer and intended path/node |
|---|---|---|
| `IDEM-01` | Concurrent same-key apply creates one transaction | cross-process `tests/test_proposal_idempotency.py::test_idem_01_apply_race` |
| `IDEM-02` | Same apply key resumes every crash phase, including WAL-before-endpoint, under one serialized full recovery envelope | fault `tests/test_proposal_idempotency.py::test_idem_02_apply_crashes` |
| `IDEM-03` | Same key changed fingerprint conflicts for every endpoint | HTTP `tests/test_proposal_idempotency.py::test_idem_03_fingerprint` |
| `IDEM-04` | Lost terminal response replays exact status/body | network fault `tests/test_proposal_idempotency.py::test_idem_04_lost_response` |
| `IDEM-05` | Result compaction to terminal tombstone never reapplies | retention `tests/test_proposal_idempotency.py::test_idem_05_compaction` |
| `IDEM-06` | Missing terminal association or uncertain/same-byte-substituted identity returns indeterminate and blocks; no new transaction is admitted | corruption `tests/test_proposal_idempotency.py::test_idem_06_uncertain` |
| `IDEM-07` | Different apply keys race to one proposal terminal winner | cross-process `tests/test_proposal_idempotency.py::test_idem_07_different_keys` |
| `IDEM-08` | Outbox failure yields at-least-once notification and one canonical mutation | fault `tests/test_proposal_idempotency.py::test_idem_08_outbox` |
| `IDEM-09` | Create crash before/after preview/proposal/result reconciles once | fault `tests/test_proposal_idempotency.py::test_idem_09_create` |
| `IDEM-10` | Supersede key binds base revision and one successor | cross-process `tests/test_proposal_idempotency.py::test_idem_10_supersede` |
| `IDEM-11` | Keyed decision appends once across crash/restart | fault `tests/test_proposal_idempotency.py::test_idem_11_decision_keyed` |
| `IDEM-12` | Keyless lost response forces canonical refetch/new CAS intent | browser+fault `tests/test_proposal_idempotency.py::test_idem_12_decision_keyless` |
| `IDEM-13` | Shared key battery accepts exact 32/128-byte charset boundaries (including structurally valid repeated-byte fixtures) and rejects 31/129 bytes, invalid punctuation, whitespace/control bytes, and Unicode identically in service, HTTP, and CLI. Entropy is a caller-generation obligation and is not runtime-testable from one key | primitive boundary `tests/test_proposal_canonical_ids.py::test_idempotency_key_exact_length_boundaries_do_not_claim_entropy`; eight-case adapter parity `tests/test_proposal_parity.py::test_idem_13_shared_key_battery` |

| Service proof | Obligation | Layer and intended path/node |
|---|---|---|
| `SERVICE-01` | Create writes coherent exact intent before domain/outbox/terminal and recovers every phase once | fault/restart `tests/test_proposal_service_recovery.py::test_service_01_create_wal` |
| `SERVICE-02` | Supersede rechecks current lineage under final lock and permits one successor across keys | cross-process `tests/test_proposal_service_recovery.py::test_service_02_supersede_uniqueness` |
| `SERVICE-03` | Keyed decision binds accepted token but fingerprints without mutable token | fault/CAS `tests/test_proposal_service_recovery.py::test_service_03_decision_fingerprint` |
| `SERVICE-04` | Keyless decision has one-shot recovery authority and requires client refetch after ambiguity | fault `tests/test_proposal_service_recovery.py::test_service_04_keyless_decision` |
| `SERVICE-05` | Withdrawal uses the same intent/token/fingerprint rules | fault/CAS `tests/test_proposal_service_recovery.py::test_service_05_withdraw` |
| `SERVICE-06` | Same-key reconciliation precedes stale-token/lifecycle evaluation | cross-process `tests/test_proposal_service_recovery.py::test_service_06_reconcile_precedence` |
| `SERVICE-07` | Startup rolls exact rows forward; prepared endpoint without coherent intent becomes durable indeterminate | crash/restart `tests/test_proposal_service_recovery.py::test_service_07_startup_recovery` |
| `SERVICE-08` | Staged/promoted previews are intent-bound and orphan cleanup preserves every reference | filesystem/fake-clock `tests/test_proposal_service_recovery.py::test_service_08_preview_lifecycle` |
| `SERVICE-09` | Capability-active gate precedes every public service read/mutation and all lower work | unit/port `tests/test_proposal_service.py::test_service_09_capability_gate` |
| `SERVICE-10` | A timeout racing final intent admission returns timeout only after proving under the complete admission locks that no authority exists; otherwise it returns/resumes in-progress authority, and no late worker may create post-timeout authority from an absent result | planned deterministic race `tests/test_proposal_async.py::test_service_10_pre_authority_timeout_race` |

| Outbox proof | Obligation | Layer and intended path/node |
|---|---|---|
| `OUTBOX-01` | Receipt stores exact canonical payload and matching digest | schema `tests/test_proposal_outbox.py::test_outbox_01_stored_payload` |
| `OUTBOX-02` | Receipt binds proposal revision, service intent/transaction, and endpoint result | contract `tests/test_proposal_outbox.py::test_outbox_02_authority_binding` |
| `OUTBOX-03` | Reconciler delivers stored payload without projector/resolver or mutable-state reads | port-denial `tests/test_proposal_outbox.py::test_outbox_03_no_reconstruction` |
| `OUTBOX-04` | Append-before-receipt crash causes at-least-once notification, never a changed payload or duplicate mutation | fault/restart `tests/test_proposal_outbox.py::test_outbox_04_append_before_mark` |

| Cursor proof | Obligation | Layer and intended path/node |
|---|---|---|
| `CURSOR-01` | Signed high waters reconstruct byte-identical continuation with process cache cleared | restart `tests/test_proposal_pagination.py::test_cursor_01_cacheless_reconstruction` |
| `CURSOR-02` | Another worker continues the same cursor from repository facts | cross-process `tests/test_proposal_pagination.py::test_cursor_02_worker_handoff` |
| `CURSOR-03` | Changed bundle/runtime/environment facts return `cursor_expired` rather than current projection | parity `tests/test_proposal_pagination.py::test_cursor_03_environment_change` |
| `CURSOR-04` | Pruned checkpoint/generation/preview/key facts return `cursor_expired` and resync | retention `tests/test_proposal_pagination.py::test_cursor_04_pruned_facts` |

| Service retention proof | Obligation | Layer and intended path/node |
|---|---|---|
| `RET-SVC-01` | Exact endpoint result retains seven days independently of 30-day preview bytes | fake-clock `tests/test_proposal_retention.py::test_ret_svc_01_result_horizon` |
| `RET-SVC-02` | Certain/indeterminate tombstones and required intent bindings survive through audit horizon | fake-clock/restart `tests/test_proposal_retention.py::test_ret_svc_02_tombstone_horizon` |
| `RET-SVC-03` | Service-WAL quota/pruning never deletes nonterminal, referenced, or indeterminate authority | quota/fault `tests/test_proposal_retention.py::test_ret_svc_03_wal_pruning` |

| Resource proof | Obligation | Layer and intended path/node |
|---|---|---|
| `RESOURCE-01` | Raw request caps precede parse/schema/capture/idempotency | adapter contract `tests/test_proposal_resources.py::test_resource_01_preparse_caps` |
| `RESOURCE-02` | Active/lineage/revision/rate/quota limits are rechecked under final locks | cross-process `tests/test_proposal_resources.py::test_resource_02_locked_limits` |
| `RESOURCE-03` | Preview workers/queue remain at 2/8 across timeout, cancellation, late success/error, heartbeat failure, lock contention, PID reuse, and process death | direct async/process nodes `tests/test_proposal_resources.py::test_resource_03_preview_bounds`; `::test_resource_03_timeout_cancel_failure_keeps_actual_worker_lease`; `::test_resource_03_repeated_timeouts_never_exceed_two_actual_workers`; `::test_resource_03_process_death_lease_expires`; `::test_resource_03_heartbeat_keeps_late_live_workers_at_capacity`; `::test_resource_03_live_identity_survives_real_heartbeat_lock_failure` |
| `RESOURCE-04` | List bounds rows and 40 MiB projection without truncation or unbounded preview expansion | exhaustion `tests/test_proposal_resources.py::test_resource_04_list_projection` |
| `RESOURCE-05` | Publication, coordinator, service, HTTP activation, CLI, and server shutdown form one idempotent ownership chain; activation failures and normal close release pinned dirfds, transaction workers, preview executors, and heartbeat threads before bundle reuse | direct lifecycle `tests/test_proposal_activation.py::test_direct_repeat_start_stop_closes_resources_and_releases_bundle`; `::test_activation_failure_closes_resources_and_releases_bundle`; `::test_server_repeat_start_stop_closes_activation_chain`; `tests/test_proposal_resources.py::test_service_close_joins_preview_executor_and_heartbeat_and_releases_root` |

| Apply proof | Obligation | Layer and intended path/node |
|---|---|---|
| `APPLY-01` | Empty accepted set returns invalid_state before WAL | unit/HTTP `tests/test_proposal_apply.py::test_apply_01_empty_set` |
| `APPLY-02` | Non-closed dependency set cannot prepare WAL | unit/HTTP `tests/test_proposal_apply.py::test_apply_02_dependency_closed` |
| `APPLY-03` | Final staged validation failure writes no content | integration `tests/test_proposal_apply.py::test_apply_03_validation` |
| `APPLY-03A` | Validation-context drift returns exact `409 proposal_conflict` with the single `validation_stale` detail defined in §16 and writes no transaction authority/content | integration `tests/test_proposal_apply.py::test_apply_03a_validation_context` |
| `APPLY-03B` | Same-byte target with different inode/parent identity conflicts before WAL | security/integration `tests/test_proposal_apply.py::test_apply_03b_same_bytes_new_inode` |
| `APPLY-04` | Preview/restage byte drift is parity failure before decision | parity/fault `tests/test_proposal_apply.py::test_apply_04_parity` |
| `APPLY-05` | Valid apply proves descriptor-relative exchange/rename and exact before/resulting identity binding; one target uses guarded direct `commit_decided → content_published`, while multiple targets prove a durable proper-prefix `publishing` state before immutable-history-linked audit and result | E2E `tests/test_proposal_apply.py::test_apply_05_success` |
| `APPLY-06` | Every pre-WAL failure leaves canonical bytes untouched | fault `tests/test_proposal_apply.py::test_apply_06_pre_wal` |
| `APPLY-07` | Failure before commit_decided restores/stays before | fault `tests/test_proposal_apply.py::test_apply_07_pre_decision` |
| `APPLY-07A` | Failure at/after commit_decided rolls forward or becomes indeterminate while recovery enforces the guarded single-target direct transition and mandatory multi-target proper-prefix transition | fault `tests/test_proposal_apply.py::test_apply_07a_post_decision` |
| `APPLY-08` | Foreign identity, same-byte inode substitution, or exchange race preserves evidence, blocks serving, and becomes indeterminate | cross-process `tests/test_proposal_apply.py::test_apply_08_foreign_bytes` |
| `APPLY-09` | Undo uses audit→immutable-history→backup identity association and restores all only when every current identity exactly equals the parent resulting identity | E2E/restart `tests/test_proposal_undo.py::test_apply_09_undo_success` |
| `APPLY-10` | One stale/pruned/ABA-changed identity restores none | fault `tests/test_proposal_undo.py::test_apply_10_undo_blocked` |
| `APPLY-11` | Missing, malformed, mismatched, or substituted undo intent/history/backup/current-resulting authority writes the exact apply-or-undo transaction-file corruption marker, blocks serving, and never restores a prefix | eleven-case corruption matrix `tests/test_proposal_transaction_security.py::test_apply_11_undo_corruption_markers` |

| Migration proof | Obligation | Layer and intended path/node |
|---|---|---|
| `MIG-01` | Valid legacy row projects exact closed identity only | fixture `tests/test_proposal_migration.py::test_mig_01_projection` |
| `MIG-02` | Malformed complete legacy row cannot project/apply | corruption `tests/test_proposal_migration.py::test_mig_02_malformed` |
| `MIG-03` | Duplicate id requires exact source generation | fixture `tests/test_proposal_migration.py::test_mig_03_generation` |
| `MIG-04` | V1 proposal full path works without legacy mutation | E2E `tests/test_proposal_http.py::test_http_01_create` |
| `MIG-05` | Future schema fails closed and remains byte-preserved | compatibility `tests/test_proposal_migration.py::test_mig_05_future` |
| `MIG-06` | Existing `/__apply` remains unchanged | regression `tests/test_proposal_direct_compat.py::test_mig_06_apply` |
| `MIG-07` | Existing mutator/CLI remains unchanged | regression `tests/test_proposal_direct_compat.py::test_mig_07_cli` |
| `MIG-08` | Disabled capability has no proposal effect | integration `tests/test_proposal_migration.py::test_mig_08_disabled` |
| `MIG-09` | Configured-root change has no implicit migration | containment `tests/test_proposal_migration.py::test_mig_09_root` |
| `MIG-10` | Intent-v1/WAL-digest correction requires no migration because no persisted production apply/undo transaction data predates it | release fixture `tests/test_proposal_migration.py::test_mig_10_pre_release_transaction_schema` |
| `MIG-11` | Proposal capability off/on leaves every existing-bundle direct writer family, option defaults, normalized stdout/stderr bytes, exit code, shared lock behavior, and resulting visible content bytes compatible; proposal-owned coordination lock files are excluded from content comparison | eleven-family subprocess and lock compatibility `tests/test_proposal_direct_compat.py::test_mig_11_cli_direct_compatibility` |

| Keyboard proof | Obligation | Layer and intended path/node |
|---|---|---|
| `KEY-01` | Skip/link entry and logical DOM order | browser `tests/test_proposal_accessibility.py::test_key_01_entry` |
| `KEY-02` | Alt operation navigation excludes editables | browser `tests/test_proposal_accessibility.py::test_key_02_operation_nav` |
| `KEY-03` | Accept confirmation semantics | browser `tests/test_proposal_accessibility.py::test_key_03_accept` |
| `KEY-04` | Reject dependent reason dialog | browser `tests/test_proposal_accessibility.py::test_key_04_reject` |
| `KEY-05` | Hunk navigation ownership | browser `tests/test_proposal_accessibility.py::test_key_05_hunks` |
| `KEY-06` | Apply has explicit button/dialog only | browser `tests/test_proposal_accessibility.py::test_key_06_apply` |
| `KEY-07` | Topmost dialog Escape/native popup ownership | browser `tests/test_proposal_accessibility.py::test_key_07_escape` |
| `KEY-08` | Decision success focus restoration/announcement | browser `tests/test_proposal_accessibility.py::test_key_08_decision_success` |
| `KEY-09` | User error focuses persistent summary only | browser `tests/test_proposal_accessibility.py::test_key_09_error_focus` |
| `KEY-10` | Reorder/removal focus fallback | browser `tests/test_proposal_accessibility.py::test_key_10_reorder` |
| `KEY-11` | Native text/browser commands preserved | browser `tests/test_proposal_accessibility.py::test_key_11_native` |
| `KEY-12` | Apply/undo success destination/announcement | browser `tests/test_proposal_accessibility.py::test_key_12_success` |
| `KEY-13` | Terminal failure destination/announcement | browser `tests/test_proposal_accessibility.py::test_key_13_failure` |
| `KEY-14` | Indeterminate alert/focus/control disablement | browser `tests/test_proposal_accessibility.py::test_key_14_indeterminate` |
| `KEY-15` | In-progress/late result never steals focus | browser `tests/test_proposal_accessibility.py::test_key_15_late` |

| Accessibility proof | Obligation | Layer and intended path/node |
|---|---|---|
| `A11Y-01` | Names, roles, statuses, issue links, live-region counts | axe+a11y tree `tests/test_proposal_accessibility.py::test_a11y_01_semantics` |
| `A11Y-02` | 320 px single-column no page overflow | browser visual `tests/test_proposal_accessibility.py::test_a11y_02_mobile` |
| `A11Y-03` | Forced colors preserves focus/status distinctions | browser visual `tests/test_proposal_accessibility.py::test_a11y_03_forced_colors` |
| `A11Y-04` | Reduced motion removes animation/smooth scroll | browser `tests/test_proposal_accessibility.py::test_a11y_04_reduced_motion` |
| `A11Y-05` | No-JS contains no proposal data/control/claim | HTTP/browser `tests/test_proposal_accessibility.py::test_a11y_05_no_js` |
| `A11Y-06` | Focus restoration across every terminal async outcome | browser `tests/test_proposal_accessibility.py::test_a11y_06_focus_matrix` |

| Crash proof | Obligation | Layer and intended path/node |
|---|---|---|
| `CRASH-01` | Before WAL has no publish authority; absent/partial/complete pre-WAL private files are classified without fabrication | fault `tests/test_proposal_crash.py::test_crash_01_before_wal` |
| `CRASH-02` | Stage/intent/history crash cleans only proved-orphan identity-matched private files and preserves ambiguity | fault `tests/test_proposal_crash.py::test_crash_02_stage` |
| `CRASH-03` | Prepared-WAL recovery, including crash before endpoint preparation, rolls back/stays at exact before identities and finalizes the same intent | fault `tests/test_proposal_crash.py::test_crash_03_prepared` |
| `CRASH-04` | Torn commit-decision replacement chooses only valid old/new | fault `tests/test_proposal_crash.py::test_crash_04_commit_record` |
| `CRASH-05` | Durable decision before first rename rolls forward | fault `tests/test_proposal_crash.py::test_crash_05_decided` |
| `CRASH-06` | Exchange/rename-before-verification/fsync reconciles exact source and destination identities, rejects same-byte substitution, fsyncs, reverifies, and only then advances | fault `tests/test_proposal_crash.py::test_crash_06_parent_fsync` |
| `CRASH-07` | Mid-publication multi-target recovery takes the full normal lock envelope, records exact resulting identities with each proper prefix, and cannot race capture/direct writers | fault/cross-process `tests/test_proposal_crash.py::test_crash_07_mid_publish` |
| `CRASH-08` | All exact resulting identities before the completion marker become `content_published` only after source/destination revalidation, by guarded direct WAL replacement for one target or from a durable proper-prefix state for multiple targets | fault `tests/test_proposal_crash.py::test_crash_08_publish_complete` |
| `CRASH-09` | Verified intent+WAL+history after publication produces byte-exact preassigned audit/activity/outbox/result rows under every candidate-prefix crash and the same intent-digest → closed-audit → endpoint/outbox topological substitution order | fault/restart `tests/test_proposal_crash.py::test_crash_09_finalize` |
| `CRASH-10` | Missing/corrupt/mismatched intent, history, audit, terminal result/tombstone, activity, or outbox is rebuilt only from an exact intent-bound candidate; otherwise evidence is preserved and corruption/indeterminate blocks startup | fault/corruption `tests/test_proposal_crash.py::test_crash_10_result` |
| `CRASH-11` | Result before WAL committed replays exact result | fault `tests/test_proposal_crash.py::test_crash_11_result_wal` |
| `CRASH-12` | Pending outbox after commit appends notification | fault `tests/test_proposal_crash.py::test_crash_12_outbox_pending` |
| `CRASH-13` | Event-before-receipt permits duplicate notification, not mutation | fault `tests/test_proposal_crash.py::test_crash_13_event_receipt` |
| `CRASH-14` | Response loss after receipt replays exactly | fault `tests/test_proposal_crash.py::test_crash_14_response_loss` |
| `CRASH-15` | Pre-decision rollback crash finishes reverse prefix | fault `tests/test_proposal_crash.py::test_crash_15_rollback` |
| `CRASH-16` | Concrete candidate checksum in an intent hash, cyclic/downstream-first substitution, checksum mismatch, missing resulting identity, or same-byte/different-inode authority after decision becomes indeterminate | fault/security `tests/test_proposal_crash.py::test_crash_16_indeterminate` |
| `CRASH-17` | Pre-WAL cleanup deletes only recognized prepare artifacts with exact recorded identity/linkage; an unknown file, symlink, hard link, or directory is preserved as evidence and blocks serving | five-case filesystem matrix `tests/test_proposal_transaction_security.py::test_crash_17_unknown_prepare_cleanup` |
| `CRASH-18` | Same-key retry recovery acquires every rank through transaction, intent/history, and activity/event/outbox in canonical order; omission or inversion is detected under cross-process contention | exact rank trace `tests/test_proposal_crash.py::test_crash_18_complete_retry_lock_rank`; contention `tests/test_proposal_crash.py::test_concurrent_same_wal_recovery_finalizes_once` |

Unit, integration, cross-process, fault-injection, HTTP, CLI, migration, and
transport E2E layers now include executable Bundle 3 evidence. Browser,
accessibility, no-JavaScript Proposal UI, apply/undo, and complete
proposal→review→apply→undo layers remain planned work.
Passing one layer does not waive another.

## 18. Production-ready staged implementation plan

No bundle below closes the feature by itself.
The capability MUST remain unavailable or visibly experimental until real Studio UI, API and CLI, apply and recovery, migration, and end-to-end proof all land.

Implementation status as of 2026-07-15 is evidence-scoped:

| Bundle/surface | Status | Permitted claim |
|---|---|---|
| Foundation Bundle 1 contracts/repository/projectors | Implemented library foundations with tests; not a product surface | Closed schema, repository, and pure projection foundations exist. Full §17 storage/recovery proof remains a release gate. |
| Foundation Bundle 2 capture/frozen validation/preview | Implemented library adapters with named tests in §6.1 | Deterministic capture, frozen validation, and pure preview composition exist; no proposal product activation follows. |
| Foundation Bundle 3 create/show/list/decide/withdraw service slice | **Implemented experimental closure** | Repository/service, bounded legacy regeneration, recovery, cursor, retention, resource, immutable-outbox, and transport-neutral apply/undo evidence execute through named nodes. |
| HTTP routes and CLI commands | **Implemented experimental opt-in** | Seven dedicated routes, matching noninteractive commands including apply/undo, dual local mutation capabilities, exact Origin/target/method guards, parity, and production Studio outbox bridge are executable. |
| Studio Proposal Review UI | **Inspection-only panel: implemented experimental opt-in**; decision workspace future/unimplemented | Local editable Studio pages list and inspect proposal revisions (read model, conflicts, operations, validation, reported retention dates, split/unified diff; apply/undo history is not reported) through the GET routes only, refetch after proposal notifications, bundle edits, live-connection recovery, and BFCache restore, and clear proposal text on pagehide; proof `tests/test_proposal_inspection_browser.py`. No decide/withdraw/apply/undo UI, browser review workspace, or public-browser lifecycle support claim. |
| Apply/recovery/undo content publication | **Implemented experimental transport-neutral backend with public HTTP/CLI adapters** | Direct tests cover the checked-in Linux exchange publication, complete recovery envelope, exact-candidate finalization, identity-bound undo, selected waiter/crash paths, and cross-process serialization; HTTP/CLI adapter tests cover success, replay, and contract errors. Unexecuted catalogue variants remain future obligations and are not implied by range labels. This is not proof of Studio UI/browser serialization, non-Linux publication, or production release safety. |

This table reports code existence, not release readiness, and does not convert planned proof paths into executed evidence.

1. **Foundation A — contracts and authority repository.** Implement closed schema/value objects and `ProposalRepositoryV1` ownership of `repository.json`, logs, sequences, checkpoints, preview artifacts, endpoint results, tombstones, corruption/repair rows, canonical transaction finalization intents, history manifests and backup identities, rotation, quotas, trusted-dirfd access, shared/exclusive bundle transaction ownership, intent/WAL/history retirement association, and startup recovery. Its only dependencies are Python baseline primitives and injected filesystem/clock/random/hash/lock ports. It MUST NOT import server, Studio, CLI, render, viewer, HTTP handlers, or a production bundle model. Keep capability inactive and prove schema/storage/idempotency authority in isolation, including required gate `tests/test_proposal_repository.py::test_store_13_history_manifest_authority`.
2. **Foundation B — pure projectors and read model.** Implement lifecycle, dependency, conflict, retention, audit, pagination, and read-model projectors over explicit immutable ports: `ProposalRepositorySnapshot`, `BundleSnapshotPort`, `RuntimeIdentityPort`, and `ClockPort`. It MUST NOT import server, Studio, CLI, render, viewer, mutation handlers, or filesystem globals. There is no production `CurrentBundleView` placeholder: tests use a contract fake, and production wiring does not land until the deterministic preview seam provides a real immutable adapter.
3. **Deterministic preview seam.** Add the real immutable bundle snapshot adapter, exact double scan, visible-symlink rejection, authority-bearing `validation_context_digest`, no-live-filesystem frozen validation, side-effect-free typed handlers, declared-access enforcement, sole content-addressed preview, before/after bytes, diff-v1, validation delta, expiry, and parity fixtures. Still no user-facing completion claim.
4. **Transport-neutral service closure, then versioned API and CLI seam.** First close the Bundle 3 service WAL/recovery, immutable outbox payload, stateless cursor, retention, resource, and capability-gate obligations without adding a transport. Then add capability discovery, dedicated routes, commands, CSRF/exposure/error precedence, cursor/read-model CAS, stable JSON, no-store behavior, and adapter parity. The coordinator proof now enables the experimental registered apply/undo adapters; product release remains gated by the full proof matrix.
5. **Apply/recovery/undo seam.** Add global lock order, trusted parent pinning, staging, immutable finalization intent before history and WAL, complete WAL with required intent digest, durable commit decision, publication, exact-candidate finalization, rollback/roll-forward recovery, endpoint results, transaction audit, at-least-once outbox, replay, retention, and every crash proof.
6. **Studio review seam.** Build the real list and review workspace, decisions, conflict and validation display, responsive diffs, keyboard/focus/announcement contract, forced colors, reduced motion, no-JS honesty, no-persistence policy, and stale-response fencing.
7. **Migration and release-proof seam.** Exercise closed legacy identity, disabled capability and direct regressions, static links, full security and accessibility matrices, E2E proposal→review→apply→undo, operational repair/recovery, and independent review.

Each bundle must be production-quality within its seam: no authority-bearing TODO/TBD, no permissive placeholder parser, no in-memory-only authority, and no route or UI that claims unsupported guarantees.

## 19. Release criteria

Proposal mode remains an explicit **experimental opt-in capability** until every P0 row in the QUALITY_PROOF_MATRIX and every named matrix proof passes against the release candidate, migration behavior is documented and exercised, static internal links and strict bundle validation pass, and an independent reviewer accepts the evidence.
Release notes MUST state that direct mode remains default, proposal mode is explicit and experimental, legacy suggestions are not applicable proposals, and audit retention differs from undo availability.

The feature is not implemented merely because schemas, storage, `proposals.jsonl`, suggestion notifications, a panel shell, API stubs, or a subset of bundles exists.
Feature closure still requires the Studio UI, public-browser lifecycle/accessibility
evidence, and full proposal→review→apply→undo E2E proof. The implemented
seven-route HTTP/CLI, activation, legacy-regeneration, and outbox surfaces MUST
be described as experimental incomplete and MUST NOT imply product-level
production safety.

## 20. Acceptance-bar self-check

This table distinguishes implemented experimental transport evidence from
future product-level proof; it is not a production-readiness claim.

| Acceptance-bar requirement | Status | Normative closure |
|---|---|---|
| Every Q1–Q37 decision resolved with no blank authority choice | **Satisfied** | §§1–19 and §21 map every decision; no decision is reopened. |
| Direct mode default; proposal review explicit opt-in | **Satisfied** | §§1–2 and compatibility proofs. |
| Persisted/wire schemas closed, versioned, bounded, and exemplified | **Satisfied** | §§3–5 include repository/repair/transaction-intent/history/audit/progress/result/cursor families, limits, examples, duplicate/depth/extension rules. |
| Complete mutually consistent proposal/decision/conflict/supersession/apply/recovery/expiry/undo machines | **Satisfied** | §§7–9, including terminal-state and commit-decision precedence. |
| Deterministic operation union, read/write sets, dependencies, blocking, final bytes | **Satisfied** | §§5.2, 6, and 7.3. |
| Snapshot/revision/no-rebase/freshness/diff binding proves reviewed equals applied | **Satisfied** | §§5.3 and 6 bind sole preview artifact, validation context, captured-only validation, and all runtime identities. |
| Exact atomic boundary, locks, staging, intent, WAL, commit, rollback, history, outbox, replay, indeterminate | **Bundle 3 metadata mutation/outbox, transport-neutral apply backend, and public HTTP/CLI adapter evidence exist** | Named service/outbox rows, backend coordinator proofs, and public HTTP/CLI adapter tests execute; Studio UI, browser proof, and full public E2E remain future. |
| Every API/CLI operation has security boundary, caps, idempotency, responses, errors, precedence | **Satisfied** | §§4, 9.1, 10, and 11; v1 correctly specifies CSRF capability and loopback mutation rather than claiming authentication. |
| Storage append/checkpoint/rotation/retention/pruning/corruption/migration and audit-vs-undo | **Satisfied** | §§5.7, 7.5, and 9 define exact `repository.json`, one repository, repair protocol, intent→WAL→history restart association, quotas, and distinct horizons. |
| Transaction progress, terminal wire states, and replay are closed | **Satisfied** | §§5.7 and 8.1 define one progress algebra and separate 202, terminal success, persisted terminal error, indeterminate, audit, and replay authorities. |
| Pagination cursor and page/proposal tokens are fully separated | **Implemented experimentally for Bundle 3** | §10 cursor/page and private HTTP proposal-token boundaries execute through named pagination/HTTP/security nodes. |
| Studio keyboard/mobile/forced-colors/reduced-motion/screen-reader/no-JS/reconnect/stale async | **Satisfied** | §§12 and 14. |
| Security excludes execution, fails closed, and keeps constraints passive | **Satisfied** | §13 plus trusted-dirfd and exposure contracts in §§2, 9, and 10. |
| Existing direct CLI/HTTP unchanged unless explicit proposal mode | **Satisfied** | §§1–2, 13, 15 and planned direct-regression proofs. |
| Proof matrices cover every state/error/race/crash/retention/malicious/accessibility/E2E obligation | **Transport/service/apply/undo evidence executed; UI/browser rows planned** | §§12 and 16–17 distinguish executable Bundle 3 nodes from future public-browser ASYNC/accessibility obligations. |
| Release language withheld until proof and independent review | **Satisfied** | §19 explicitly forbids implementation/support/readiness claims before closure. |

The document is therefore ready for a fresh independent specification review.
The experimental Bundle 3 transport satisfies its named evidence; complete
product conformance remains unproven until remaining UI/browser proofs execute
and independent evidence review accepts them.

## 21. Decision closure

Q1–Q37 are resolved by §§1–19 without exceptions: opt-in posture (§§1–2), immutable identity and schemas (§§3–5), typed operations and dependencies (§§5–7), decisions and supersession (§7), conflicts/no-rebase (§§6–7), atomic apply/idempotency (§§8, 16), storage/retention (§§7, 9), API/CLI/pagination (§§10–12), Studio/accessibility (§14), security/governance (§13), notification/audit/undo (§§5, 7–8, 12), migration/direct compatibility (§§2, 9, 15), required proof (§§16–17), and experimental release posture (§§18–19).
There is no authority-bearing open choice in protocol v1.
