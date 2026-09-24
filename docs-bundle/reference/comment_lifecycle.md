---
type: Reference
title: Comment lifecycle and archive rules
description: The full current comment state model — states, the separate archive track, the request_summary/summary pair, ts vs updated_at, threading, and server-side archive rules.
resource: /reference/comment_lifecycle.md
tags: [comments, studio, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# Comment lifecycle and archive rules

## Document identity and shared commenters

Page feedback can target `document:"path.md"`, including indexes and logs, with
`anchor:{"kind":"page"}` or an omitted anchor. New rows/events retain `document`;
reserved rows have `concept:""`, not a fabricated concept. Legacy concept rows
and exact mapped anchors are unchanged. See the [HTTP document contract](http_routes.md#document-targets)
for filtering, reads and bootstrap fields. Do not downgrade a session containing
page rows to an older runtime that cannot read reserved-document targets.

Parent/reply identity, durable replay, claim, agent clarification replies,
resolve and archive rules still apply. CLI replies inherit the document, and
`wait` delivers it with the ask. Page feedback has no highlight or re-anchor,
even if its owning document disappears. Public comment-only readers may create
or reply as users only, not update rows, claim/resolve, manipulate presence or
edit content. Replies may auto-unarchive their parent under the existing rule.
There are no reader accounts or “edit own” permissions.

A **comment** (also called a **directive**) is a user-authored ask
that lands in `<bundle>/.okf-loom/session/directives.jsonl`. Comments
drive the live studio's comment-driven development loop: the user
directs via comments, the agent consumes them via
[`scripts/okf-loom wait`](cli.md#wait), and the loop closes when the agent
resolves the comment.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

This document describes the current okf-loom 2.0.0 state model. The OKF 0.2
format upgrade does not renumber comment/storage schemas.

# Two independent tracks

A comment carries two independent pieces of state:

| Track | Field | Values |
|---|---|---|
| **Lifecycle state** | `state` | `open` / `claimed` / `resolved` / `dismissed` |
| **Archive flag** | `archived` | `true` / `false` |

Archive is a **soft hide**, not a lifecycle state. Archiving a
resolved comment keeps it resolved; unarchiving restores the prior
visibility without flipping state. The two tracks never interact
except through the server-side archive gate (see below).

# Lifecycle states

| State | Meaning | Who sets it |
|---|---|---|
| `open` | New user comment; not yet picked up. Default on creation. | User (`POST /__comment`) or reopen. |
| `claimed` | Agent has picked it up and is working on it. Sets `claimed_by`. | Agent ([`scripts/okf-loom comment-claim`](cli.md#comment-claim)). |
| `resolved` | Agent completed the work and posted a reply + activity links. | Agent ([`scripts/okf-loom comment-resolve`](cli.md#comment-resolve)). Agent replies from [`comment-reply`](cli.md#comment-reply) are born `resolved`. |
| `dismissed` | User cancelled an unclaimed comment. | User (`POST /__comment-update {state:"dismissed"}`). |

## State diagram

```mermaid
stateDiagram-v2
    [*] --> open : POST /__comment
    open --> dismissed : /__comment-update {state:"dismissed"}
    dismissed --> open : /__comment-update {state:"open"} (reopen)
    open --> claimed : scripts/okf-loom comment-claim
    claimed --> resolved : scripts/okf-loom comment-resolve --reply --activity
    resolved --> open : /__comment-update {state:"open"} (re-open wakes wait_for_work)
    note right of claimed
        server refuses dismiss: 409
        (agent is mid-flight)
    end note
```

The agent never dismisses; the agent resolves. The user never
claims or resolves; the user dismisses or reopens.

# Transitions and timestamps

Each comment carries two timestamps:

| Field | Mutability | When it changes |
|---|---|---|
| `ts` | Immutable | Set once at creation. |
| `updated_at` | Bumped on every transition | Claim, resolve, dismiss, reopen, archive, unarchive, reply, summary update, body edit. |

`scripts/okf-loom wait` treats a re-opened comment as fresh work even though its
`id` is unchanged: it tracks the latest `updated_at` (with a
compatibility fallback to `ts` for older records).

## Notification attribution is not comment authorship

The directive row's `actor` and `ts` continue to describe its creation. A new
lifecycle event in the existing event feed carries `operation:"lifecycle"`;
its `actor` identifies the supplied acting label and its `ts` equals the
new row's `updated_at`. Claim/resolve use the explicitly supplied `claimed_by`
label (including named agents); admitted HTTP body/archive/state updates use
`user`. A trusted library update with no actor evidence emits `actor:null`,
not the original author or a retained claim owner. Its canonical `row_token`
still identifies the resulting row. An implicit parent unarchive caused by a
reply or attributed lifecycle action carries that same acting label.

Legacy events are not backfilled or rewritten, and absent legacy creation
authors/times stay absent in rows. Create/re-anchor notification receipts retain
their immutable row-snapshot shape and recovery/idempotency behavior. Consumers
must not interpret an older unmarked event as attribution of a claim/resolve.

For availability, `claimed_at` dates a current claim; an agent-authored reply's
`ts` dates that reply; an explicitly attributed lifecycle/activity event dates
that action. A retained `claimed_by` proves past ownership but not when an agent
last acted: subsequent archive/body updates also change `updated_at`. Unknown
times stay unknown. None of this proves an external worker is currently running,
and no presence flag, event actor label or timestamp grants authority.

# Body edits (open/claimed only)

The comment author can amend the text of a not-yet-resolved comment in
place: `POST /__comment-update` with a non-empty `body`. The studio UI
shows an Edit button on user-authored, not-yet-resolved comments.

| Rule | Detail |
|---|---|
| Allowed states | `open` / `claimed` only. `409` "cannot edit a resolved/dismissed comment (reopen it first)" on `resolved`/`dismissed`. |
| Empty body | `400`. |
| Compare-and-swap | `expected_row_token` is required and must match the latest row under lock. |
| `updated_at` | Bumped — an agent blocked in [`scripts/okf-loom wait`](cli.md#wait) re-receives the corrected ask as fresh work. |
| `request_summary` | Re-derived from the new body, so the collapsed preview tracks the edit. |

# The `summary` field

A short agent-authored blurb shown in the collapsed thread preview.

```bash
scripts/okf-loom comment-claim   <bundle> <id> --summary "adding depends_on + Order entity"
scripts/okf-loom comment-resolve <bundle> <id> --summary "done: link + entity added" \
                                --reply   "Done — added depends_on + Order entity" \
                                --activity 01JABED5K7,01JABED5K8
```

`summary` is separate from `reply` (the long-form resolution
message). The collapsed preview shows the actor prefix
(`You:` / `Agent:`), the body ellipsis, and the summary chip; the
user expands the thread to read the full reply.

# Threading (2-level cap)

A comment may carry a `parent_id` linking a reply to its parent.
The studio renders up to **2 levels of nesting**
(parent → reply → reply-to-reply); deeper nesting is hoisted to root.

| Property | Detail |
|---|---|
| `parent_id` | Set on the reply; the reply is anchored to the parent's concept. |
| 2-level cap | Parent → reply → reply-to-reply. Further replies hoist to root. |
| Anchor inheritance | A reply inherits the parent's `concept`. |

Use replies to ask the user a clarifying question on an existing
directive rather than opening a new top-level comment.

## Agent replies (`comment-reply`)

The agent-side channel is
[`scripts/okf-loom comment-reply`](cli.md#comment-reply). The reply is
posted under the thread **root** (replying to a reply hoists to the
root, matching the display cap) and never touches the parent's
lifecycle state — reply-without-resolve is the point. It is created
with `state=resolved` and `claimed_by=<actor>` (a statement, not an
ask), so [`wait --for comment`](cli.md#wait) never returns the agent
its own reply and the all-resolved archive gate stays satisfiable
without the agent resolving its own remarks. The user's answer — their
reply — wakes `wait` as new work.

# Archive rules

## Separate track

`archived` is a **boolean field**, not a lifecycle state. Archive
does not change `state`; it only flips the boolean the UI uses to
hide the thread.

| Operation | Effect on `state` | Effect on `archived` |
|---|---|---|
| Archive | unchanged | `true` |
| Unarchive | unchanged | `false` |

## Server-side rules

| Rule | Failure response |
|---|---|
| Archive only on the **root** comment of a thread. | `400` "archive is only available on the top-level comment of a thread". |
| Archive only when **every** comment in the thread (root + all replies) is `resolved`. | `409` with `unresolved_ids: [...]`. |
| Dismiss a `claimed` comment. | `409` "cannot dismiss a claimed comment". |
| Send a non-boolean `archived` value. | `400`; values are never truthiness-coerced. |

The UI disables the Archive button client-side with a tooltip until
the whole-thread-resolved gate is met; the server enforces the same
rule so a hand-crafted POST cannot bypass it.

## Auto-unarchive on reply

Replying to an archived thread **auto-unarchives the parent**. The
reply itself is never archived. Adding to a thread reopens it for
business; archive is a soft hide, not a closed state.

This is enforced in `Studio.post_comment`: when `parent_id` points
at an archived parent, the parent is unarchived before the reply is
appended.

## Compatibility normalisation

Some older records used `state == "archived"` as a lifecycle state
before archive became a separate track. On read, these are
normalised to `archived=true, state="open"` (best-effort
migration). The on-disk record is not rewritten; normalisation is
view-only.

# Reading comments

| Source | API |
|---|---|
| CLI | [`scripts/okf-loom comment-list <bundle>`](cli.md#comment-list) — filters `--state`, `--concept`. |
| HTTP | `GET /__comments` — see [http_routes.md](http_routes.md). |
| Library | `Studio.list_comments(state=…, concept=…)` / `Studio.get_comment(id)`. |

`directives.jsonl` is append-only: a transition appends a new full
record for the same `id`. Readers keep only the latest record per
id (last-write-wins), merging retained generations with the durable
`directives-checkpoint.json` latest-row snapshot. Rotation checkpoints every
comment's latest complete row before pruning; checkpoint/prune failure retains
old generations. Before a later append, an incomplete crash-torn active
suffix is truncated under the shared lock; complete malformed rows and
indeterminate rotated history remain fail-closed rather than being hidden.

# JSON shape of a comment record

A single record as returned by `GET /__comments` or
`scripts/okf-loom comment-list --format json`:

```json
{
  "id": "01JABED9K0",
  "ts": "2026-06-29T18:12:39Z",
    "updated_at": "2026-06-29T18:14:02Z",
    "row_token": "r2:01K0ROW8N7Q2",
  "concept": "tables/orders",
    "anchor": {"kind": "text", "ref": "customer_id"},
  "actor": "user",
  "body": "Add a depends_on relation to customers.",
  "state": "resolved",
    "claimed_by": "agent",
    "claimed_at": null,
  "resolved_activity": ["01JABED5K7", "01JABED5K8"],
  "reply": "Done — added depends_on + Order entity",
  "request_summary": "ask: add depends_on relation",
  "summary": "done: link + entity added",
  "detail": {"intent": "add_relation"},
  "archived": false,
  "parent_id": null
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | string | ULID; sortable as a string. |
| `ts` | string (ISO 8601) | Immutable creation timestamp. |
| `updated_at` | string (ISO 8601) | Bumped on every transition. |
| `row_token` | opaque ASCII string | Stable identity of this latest full row; changes on every append and is the concurrency token. Legacy rows receive a deterministic `legacy:` token on read. |
| `concept` | string | Concept id the comment is anchored to. |
| `anchor` | object | Legacy `{kind:"text",ref}` or the closed version-2 text-quote schema in [spec §12.1](spec.md#121-annotations-v2-contract-normative-implemented). May be `{}` for historical records; unknown stored fields are preserved. |
| `actor` | string | `user` (default) or `agent`. |
| `body` | string | The comment text. |
| `state` | string | `open` / `claimed` / `resolved` / `dismissed`. |
| `claimed_by` | string \| null | Set on claim; cleared on reopen. Agent replies born `resolved` set it at creation. |
| `claimed_at` | string (ISO 8601) \| null | Set when a claim is acquired or its owner changes, preserved across unrelated claimed-row updates, and cleared when leaving `claimed`. Malformed retained values are visibly quarantined. |
| `resolved_activity` | list of strings | Change-list entry ids linked to the resolution (group undo). |
| `reply` | string \| null | Long-form resolution message (set on resolve). |
| `request_summary` | string \| null | Short blurb for the ASK — written by `comment-claim --summary`, auto-derived from the first line of `body` when absent. |
| `summary` | string \| null | Short blurb for the DONE — written by `comment-resolve --summary`. |
| `detail` | object | Free-form metadata (e.g. Quick Action intent). |
| `archived` | bool | Archive flag. Independent of `state`. |
| `parent_id` | string \| null | Parent comment id for a threaded reply. |
| `portable_import` | object (optional) | Server-owned provenance of a locally imported portable feedback note: `{schema:1, note_id, note_sha256, document, source_sha256, claimed_author?, claimed_at?, quote?}`. Present only on rows created by `POST /__feedback-import/confirm`; see below. |

## Imported portable feedback notes

`POST /__feedback-import/confirm` creates an ordinary **open** page-anchored
comment (`anchor:{kind:"page"}`, `detail:{}`). Its `actor` is `user`, the local
Studio user, the same value the local `/__comment` path records. The claimed
author, time, and quote from the offline note are kept only inside
`portable_import` as **unverified** claims. The note origin is unauthenticated,
and byte-identical page copies are indistinguishable.

Top-level `portable_import` is the only trustworthy provenance discriminator,
because only the local import route sets it. `/__comment` rejects it as an
unknown key, `/__comment-update` and re-anchor use closed schemas, and CLI
writers and events never carry it. A `detail.portable_import` value is ordinary
untrusted detail and must be ignored. Lifecycle transitions, body edits,
re-anchor, and archive copy the row, so the marker survives them unchanged; it
describes creation provenance, not the current body. The marker is not
projected into live comment events or receipts, whose schema is closed; clients
read it from `/__comments` rows. Older servers show such rows as ordinary
comments. See [spec §12.2](spec.md#122-portable-page-level-feedback-import-local-operator-only)
for the note schema, check order, and replay-retention limits.

## Annotations v2 lifecycle integration

Anchor health
(`resolved`, `stale`, `ambiguous`, `invalid`) is derived independently from the
comment lifecycle (`open`, `claimed`, `resolved`, `dismissed`) and archive flag.
Claiming, resolving, replying, editing the body, and archiving must preserve the
latest anchor and unknown row fields. Explicit re-anchor changes only `anchor`
and row metadata, never lifecycle state, body, archive, or Markdown undo history.

Every writer uses the same per-comment cross-process lock: load the latest row
inside the lock, validate the stable `row_token` when supplied, merge only
owned fields, generate a new token, and atomically append a complete row.
`updated_at` is not a compare-and-swap token. Two re-anchors cannot both commit
from one expected token; lifecycle/body writes racing re-anchor retain each
other's owned fields. See the exact schema, resolver, state, async, cache,
keyboard, validation, and security matrices in [spec §12.1](spec.md#121-annotations-v2-contract-normative-implemented),
and the mutation envelope in [HTTP routes](http_routes.md#comment-reanchor).

Public create and update requests are closed schemas. Create rejects unknown or
duplicate keys and wrong field types. Update requires one matching row token and
exactly one typed operation—state, archive, or body. All creates/replies,
including keyless ones, receive a durable outbox identity; normal and recovered
events use the same full payload, and pending delivery is reconciled before
create-WAL retirement.
Public HTTP create is always `actor:"user"`; agent authorship belongs to trusted
CLI workflows. Public reopen is exactly `dismissed → open`; claimed or resolved
rows cannot transition directly to open. Pending notification receipts persist
and validate the immutable original full payload, so later lifecycle changes do
not rewrite the create event recovered after a fault. Re-anchor uses the same
full-row event shape on normal and recovery paths.
Every lifecycle event carries the fresh row token from its newly appended row,
so subscribers can advance CAS authority without reconstructing it.
Claim acquisition writes independently owned `claimed_at`; unrelated updates
preserve it, owner changes replace it, and leaving `claimed` clears it. Invalid
claimed timestamps remain visibly quarantined. Keyless notification identities
compact into a bounded durable checkpoint across pruning and restart.
Legacy checkpoint formats without complete membership authority fail closed.
Receipt recovery pins the session directory and receipt directory in order and
retains that single trusted receipt dirfd across enumeration, reads, cleanup,
and reconciliation. It rejects pathname/symlink substitution and securely
removes only the constrained crash-residue temporary format.

`GET /__comments` adds a derived `anchor_resolution` to each applicable row.
It is revision-qualified and reports one of `resolved`, `stale`, `ambiguous`,
or `invalid`; it is never written to `directives.jsonl`. Only `resolved` enables
Jump/highlighting. Legacy text anchors are deliberately unique-only: one exact
occurrence resolves, none is stale, and multiple are ambiguous. Reading never
upgrades a legacy anchor. The first explicit **Re-anchor** save appends a v2
row while preserving lifecycle, body, archive, and unknown stored fields.

This canonical route remains available to HTTP clients, but the no-JavaScript
concept page does not call it or render comment rows. In that mode users see
authored concept content and one fallback notice only; comment state, quote
text, anchor health, Jump, and Re-anchor require the JavaScript Studio client.

# Storage and rotation

| File | Location | Rotation |
|---|---|---|
| `directives.jsonl` | `<bundle>/.okf-loom/session/directives.jsonl` | 2 MiB active cap, 3 files kept. Comment spam cannot fill the disk. |
| `events.jsonl` | `<bundle>/.okf-loom/session/events.jsonl` | 8 MiB active cap (`studio.events_max_bytes`), 7 files kept (`studio.events_keep`). |
| `history/<hex>/<rev>.md` | `<bundle>/.okf-loom/session/history/` | Ring-capped; pruned entries surface as `404 snapshots_pruned` on undo. |
| `.comment-create-wal/` | `<bundle>/.okf-loom/session/.comment-create-wal/` | Bounded crash-consistent create replay and notification-delivery state; recovery produces one directive and exactly one transaction event. |
| `.comment-reanchor-wal/` | `<bundle>/.okf-loom/session/.comment-reanchor-wal/` | Bounded crash-consistent re-anchor replay metadata and fail-closed tombstones. |
| `.directive-transaction-results/` | `<bundle>/.okf-loom/session/.directive-transaction-results/` | Bounded exact transaction outcomes retained through the WAL/tombstone horizon. |
| `.notification-receipts/` | `<bundle>/.okf-loom/session/.notification-receipts/` | Pending/committed notification identities; event pruning checkpoints committed receipts. |

# Staleness recovery

A dropped agent process is auto-recovered by the watcher sweep:

| State | Reverted to | After |
|---|---|---|
| Agent presence (`editing`/`thinking`/`watching`) | `idle` | Fixed runtime constant: 300 seconds. |
| A `claimed` comment | `open` | Fixed runtime constant: 600 seconds from the claim transition. |

No directive is orphaned if the agent crashes mid-task.

# Workflow

The canonical agent loop:

```bash
scripts/okf-loom wait           <bundle> --for comment     # foreground block-once
scripts/okf-loom comment-claim  <bundle> <id> --summary "…"
scripts/okf-loom comment-reply  <bundle> <id> --body "Did you mean X or Y?"  # only when the ask is ambiguous
scripts/okf-loom presence       <bundle> --state editing --focus tables/orders --message "linking 3 of 7 tables…"
# …mutators (link-add / entity-add / update) with --group-id PASS1…
scripts/okf-loom comment-resolve <bundle> <id> --reply "Done — …" \
       --activity 01JABED5K7,01JABED5K8 --summary "done: …"
```

The comment `wait` returns carries a `queue: {pending: N, ids: [...]}`
field — the other open comments still pending — so the agent sees
backlog depth without diffing `comment-list` between loop turns; the
one-work-item contract (block once, print one item, exit) is
unchanged.

See [/tutorials/author_with_agent.md](/tutorials/author_with_agent.md)
for the end-to-end walk-through and
[/how-to/archive_threads.md](/how-to/archive_threads.md) for the
archive/unarchive workflow.

# See also

- [Annotations v2: a hands-on Studio walkthrough](/demo/annotations_v2_showcase.md) — try every behavior on this page live, including comments, resolution, and re-anchor.
- [index.md](/reference/index.md) — reference quadrant index.
- [cli.md](cli.md) — `wait`, `comment-claim`, `comment-reply`, `comment-resolve`, `comment-list`, `presence`, `token`.
- [http_routes.md](http_routes.md) — `/__comment`, `/__comment-update`, `/__comments`, `/__events`.
- [config_yaml.md](config_yaml.md) — implemented Studio configuration and rotation caps. Presence/claim expiry constants are not configurable.
- [/tutorials/author_with_agent.md](/tutorials/author_with_agent.md) — the agent loop tutorial.
- [/tutorials/live_studio_basics.md](/tutorials/live_studio_basics.md) — first studio session.
- [/how-to/archive_threads.md](/how-to/archive_threads.md) — archive/unarchive workflow.
- [/explanation/live_studio_design.md](/explanation/live_studio_design.md) — why archive is a separate track.
- `okf-loom/scripts/okf_loom/studio.py` — authoritative source (`post_comment`, `update_comment`, `resolve_comment`, `wait_for_work`).
