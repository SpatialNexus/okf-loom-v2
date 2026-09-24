---
type: Reference
title: HTTP server routes
description: Every route the live studio exposes — method, purpose, body shape, response shape, and status codes.
resource: /reference/http_routes.md
tags: [http, studio, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# HTTP server routes

`scripts/okf-loom serve <bundle>` runs a `ThreadingHTTPServer` with the
`OKFWikiHandler` request handler. The handler serves read routes
unconditionally and — when a studio session is live (`.okf-loom/session/`
exists) — accepts mutating POST routes behind a per-session CSRF
token and an Origin/Host allow-list.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

Default bind is `127.0.0.1:8787`. A non-loopback bind (`--public` or
a non-loopback `--host`) requires `--public-ack` or an interactive
y/N ack. See [cli.md § serve](cli.md#serve).

# Route summary

The table below covers the direct Studio/read surface. Experimental proposal
create/list/show/decide/withdraw/apply/undo routes live under `/__proposal/v1/`
and are defined in the [proposal extension](/reference/proposal_diff_review.md).
Their owner is `proposal_review/http.py`, with explicit activation in
`proposal_review/activation.py`; focused seams are `tests/test_proposal_http.py`
and `tests/test_proposal_activation.py`. Studio's opt-in, inspection-only Proposals
panel reads only the list and show GET routes with the page's session token
(`tests/test_proposal_inspection_browser.py`). This does not expose a browser
decision or apply workflow or change direct mode's default. Session state containment/lifecycle
is owned by `session_authority.py` and Studio; see the
[architecture reference](/reference/architecture.md#session-and-proposal-ownership).

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Root index page (top-level concepts + subdirs + graph link). |
| GET | `/<concept_id>` | Concept page (rendered MD, frontmatter, backlinks, outgoing, local graph). |
| GET | `/<dir>/` | Directory index page. |
| GET | `/__graph` | Full-page Cytoscape.js force-directed graph. |
| GET | `/__search` | Read-only six-mode search (`&format=json` for a compatibility result array). |
| GET | `/__search-form` | Read-only native-form adapter: omit empty/inactive filters, then 303 to strict `/__search`. |
| GET | `/__context` | Read-only deterministic schema-v1 Agent Context Pack as JSON or Markdown. |
| GET | `/__raw/<concept_id>` | Raw markdown body. |
| GET | `/__data/graph.json` | Graph JSON for external tools. |
| GET | `/__data/content.json` | Full content index JSON. |
| GET | `/__data/doc?id=` | One concept's rendered data plus canonical annotation source/revision and DOM projection map. |
| GET | `/__data/page?document=` | Bounded canonical Markdown page read, including indexes/logs; see document targets below. |
| GET | `/log.md`, `/<dir>/log.md` | Reserved log reading surface with live document bootstrap. |
| GET | `/__data/events` | Filtered change-list read over `events.jsonl`. |
| GET | `/__events` | SSE stream of change/presence/activity/comment events. |
| GET | `/__comments` | Canonical comment state from `directives.jsonl`. |
| GET | `/__feedback-import` | Local operator only: page to preview and import one portable feedback note. |
| GET | `/__diff` | Line diff between two revs of one concept. |
| GET | `/__static/<file>` | Static assets (JS/CSS; bundle overrides first when active-code is on). |
| GET | `/<path>.<media ext>` | Bundle-local media file (screenshots, diagrams, video, PDF). |
| POST | `/__comment` | User's comment / threaded reply → `directives.jsonl`. |
| POST | `/__comment-update` | User-facing state/archive transition, or a body edit of a not-yet-resolved comment. |
| POST | `/__comment-reanchor` | Explicitly replace one comment anchor with a version-2 text-quote anchor. |
| GET | `/__presence` | Canonical v2 multi-session snapshot plus a page-scoped browser capability (`no-store`). |
| POST | `/__presence` | Legacy agent update, or capability-bound browser upsert/leave. |
| POST | `/__apply` | Run a whitelisted `UpdateOp` via the studio write funnel. |
| POST | `/__undo` | Restore a prior snapshot (single or group). |
| POST | `/__preview` | Render arbitrary in-flight markdown. |
| POST | `/__tunnel` | Server admin: attach/detach/status a read-only-by-default cloudflared quick tunnel at runtime. |
| POST | `/__feedback-import/preview` | Local operator only: side-effect-free preview of one portable feedback note. |
| POST | `/__feedback-import/confirm` | Local operator only: import a previewed note as an ordinary open page comment. |

Routing normalises a trailing slash (except root) and a trailing
`.md` on concept routes (except `/index.md` and `<dir>/index.md`,
which are reserved). So `/tables/customers.md`,
`/tables/customers`, and `/tables/customers/` all resolve to the
same concept page.

# Authentication model

The search-form adapter accepts only `q`, `mode`, `type`, `tag`, `entity`,
`relation`, `source`, `target`, and `limit`, each at most once. Native form
encoding of slash-delimited concept ids is normalized; duplicate/unknown fields
and malformed encoding fail through the strict search error path. The final
search validates mode, values, and bounds. This presentation adapter is not a
permissive alternate JSON API and does not change direct `/__search` validation.
See [the search contract](/reference/spec.md#6-search-modes-and-zero-dependency-retrieval).

| Surface | Guard |
|---|---|
| Every GET | None (loopback bind by default). |
| `GET /__diff` | Per-session CSRF token (`X-OKF-Token`). |
| Local/admin POST | Per-session admin/CSRF token **AND** a non-empty allow-listed `Host`; when `Origin` is present it must also be allow-listed. |
| Public tunnel reads | Dedicated generation-bound ingress listener; read-only by default. Request headers do not classify ingress. |
| Explicit public tunnel mutation | Separate public mutation token, accepted only on `/__comment`, `/__comment-update`, `/__comment-reanchor`, `/__apply`, and `/__undo`. |
| Explicit public comment-only | `--allow-tunnel-comment` / start-only `allow_public_comment:true`: distinct generation-bound token accepted only on `/__comment` create/reply. No update/re-anchor/content/agent/admin authority. |
| Portable feedback import (`POST /__feedback-import/preview`, `/confirm`) | Admin/CSRF token + Host/Origin, an editable Studio, and a local session: loopback socket ends on the private listener with no attached tunnel, no public ingress, and no non-loopback bind (`403 local_only`). Never public. |

The token is generated on `scripts/okf-loom serve` boot, written to
`<bundle>/.okf-loom/session/.token` (mode `0600`), embedded in every
served HTML page (CSP-safe JSON data block), and read by the
studio client which attaches it as `X-OKF-Token` on every write.
[`scripts/okf-loom token <bundle>`](cli.md#token) prints it for direct HTTP use;
the CLI mutators attach it automatically.

`allowed_hosts` defaults to `("127.0.0.1", "localhost")` and is
never empty (fail-closed). `Host` is mandatory for every POST. `Origin`
remains optional for authenticated CLI clients, but when supplied it is
validated in addition to Host. A `user` actor's presence write only
takes effect when the current agent presence is already `idle` or
`watching`, so a browser boot cannot clobber an active agent.

`--no-edit` flips the studio into a read-only kiosk: live reads
stay on, every POST returns `403` — except `/__tunnel`, which is
server admin (not a bundle write; sharing a read-only kiosk is its use
case) and stays available. It is never exempt from the token +
Origin/Host guard, and a no-edit server rejects public-edit opt-in.

An active quick tunnel targets a dedicated loopback ingress listener. Its
server-owned listener identity and generation capability—not `Host`, `CF-*`, or
`X-Forwarded-*`—classify every request as public. Missing or stale state fails
closed. Public requests default to read-only across all unsafe methods and
routes, even with spoofed local-looking headers or a copied local admin token.
Public HTML omits all mutation tokens and advertises `edit: false`; loopback
editing is unaffected. With explicit `--allow-tunnel-edit`, public HTML receives
a separate generation-scoped token accepted only by the five comment/content
mutation routes listed above. Public ingress still cannot access `/__tunnel`,
proposal review, `/__preview`, `/__presence`, or sensitive diff/admin routes.

# GET routes

## `/`

Root index page. Renders the bundle-root `index.md` (hand-authored
or generated) plus the type-grouped concept listing. The studio
bootstrap (token + asset links) is injected into the `</head>`.

## `/<concept_id>`

Concept page. `concept_id` is the dotted path without `.md`
(e.g. `/tables/orders`). Returns 404 for an unknown concept unless
the path matches a directory containing concepts, in which case it
falls back to the directory index page.

The page carries: rendered body HTML, frontmatter, the "Cited by"
backlinks panel, outgoing links, and a local-graph widget.

## `/<dir>/`

Directory index page for the subdirectory `<dir>`. Renders the
`<dir>/index.md` if present, else a generated listing. Path
segments are validated; `..`, `.`, and NUL bytes return 404.

## `/__graph`

Full-page Cytoscape.js force-directed graph of the whole bundle.
The graph data comes from `/__data/graph.json`.

## `/__search`

```http
GET /__search?q=customer&format=json
```

| Query param | Effect |
|---|---|
| `q` | Query, at most 4096 Unicode code points; longer or forbidden-control input is rejected, never truncated. |
| `format` | `html` (default) or `json`. |
| `mode` | `lexical` (default), `semantic`, `hybrid`, `tag`, `entity`, or `relation`. |
| `type`, `tag` | Filters accepted in every mode; each string is at most 256 code points. |
| `entity` | Entity query/filter; valid only in entity mode and mutually exclusive with non-empty `q`. |
| `relation`, `source`, `target` | Edge filters; valid only in relation mode. Source/target are canonical concept ids. |
| `limit` | Result limit, default `20`, inclusive range `1..100`. |

Unknown and duplicate scalar parameters, malformed percent/UTF-8 encoding,
noncanonical integers, invalid bounds, unsupported formats/modes, and
mode-inapplicable parameters return `400`; a syntactically valid absent
source/target concept returns `404`.
There is no silent fallback to lexical ranking.

HTML success and errors are server-rendered.
JSON success deliberately remains a bare array for compatibility and preserves
the established `SearchResult` keys while adding bounded `detail`, `backends`,
`reasons`, and schema-v1 exact-passage `evidence`.
JSON errors use the stable retrieval error envelope shown below.
Responses are capped at 4 MiB; deterministic tail omission is reported by
`X-OKF-Omitted-Count`, or an indivisible oversized response returns `413`.

## `/__context`

```http
GET /__context?q=customer&mode=hybrid&budget_tokens=6000&neighbors=1&limit=20&format=json
GET /__context?concept=reference/http_routes&format=markdown
```

| Query param | Effect |
|---|---|
| `q` | Query seed, at most 4096 code points. Required unless `concept` is supplied. |
| `concept` | Canonical existing concept seed. When `q` is absent, its title becomes the ranking query. |
| `format` | `json` (default) or `markdown`. |
| `mode` | One of all six modes; default `hybrid`. |
| `type`, `tag`, `entity`, `relation`, `source`, `target` | Same bounds and mode-applicability rules as `/__search`. |
| `limit` | Candidate-item limit, default `20`, range `1..100`. |
| `budget_tokens` | Estimated-token budget, default `6000`, range `128..50000`. |
| `neighbors` | Graph-neighbor depth, default `1`, range `0..2`. |
| `selection_exact` | Exact source selection, at most 4096 code points; requires `concept`. |
| `selection_prefix`, `selection_suffix` | Optional disambiguation, each at most 512 code points. |
| `selection_heading` | Optional exact heading discriminator, at most 256 code points. |

JSON success is the deterministic schema-v1 pack.
Markdown success is exactly its canonical `representations.markdown` value;
both select the same items.
The 4 MiB cap never byte-cuts either representation.

Success returns `200` with `ETag`, `X-OKF-Bundle-Revision`, and
`Cache-Control: private, no-cache`.
Malformed/missing seeds, validation failures, absent selections, or an exact
selection that cannot fit its budget return `400`; an absent concept returns
`404`; an ambiguous exact selection returns `409`; an impossible response cap
returns `413`; an oversized raw request target returns `414`; and retrieval
capacity/deadline failures return `503`.
All context errors are the stable JSON envelope, including when Markdown was
requested.

Search and context are replay-safe, read-only operations.
Each captures one immutable bundle snapshot for ranking, passages, revision,
headers, and serialization, so a concurrent bundle swap cannot tear a response.
See [context_packs.md](/reference/context_packs.md).

## `/__raw/<concept_id>`

Raw markdown body of one concept (`Content-Type: text/markdown`).
Used by popover previews and agent copy/paste. Returns 413 if the
body exceeds `MAX_RAW_RESPONSE_BYTES`.

## `/__data/graph.json`

Graph JSON: nodes (one per concept) + edges (one per internal link,
including wikilinks with `form="wikilink"`). Same shape as
[`graph --format json`](cli.md#graph).

## `/__data/content.json`

Full content index JSON: every concept's id, type, title,
description, tags, and outgoing link targets. Same shape as the
`.okf-loom/index/content.json` derived artifact emitted by
[`index --emit-json`](cli.md#index).

## `/__data/doc?id=<concept_id>`

One concept's rendered + raw + metadata JSON for in-place re-render
(the §6 no-refresh live update path).

OKF 0.2 adds `metadata` (normalized read-only fields) and `metadata_html`
(escaped advisory presentation). Existing raw frontmatter, annotation source,
projection, and revision contracts remain distinct and unchanged. Generation,
verification, lifecycle, and computation display never grants mutation authority
or runs an executor/attester. Graph nodes expose `okf_metadata`, `metadata_html`,
and `generated_at` without replacing the older grouping `metadata` field.

```json
{
  "id": "tables/orders",
  "rev": "<content-hash>",
  "title": "Orders",
  "type": "Table",
  "html": "<rendered body HTML>",
  "raw": "<raw markdown body>",
  "frontmatter": { "type": "Table", "title": "Orders", "..." : "..." },
  "headings": [{"level": 1, "text": "Schema"}, "..."],
  "backlinks": ["services/checkout"],
  "outgoing": ["tables/customers", "tables/products"]
}
```

Concept responses also carry `annotation_source` (exact canonical Markdown),
`concept_content_rev` (lower-case SHA-256), and `annotation_projection`.
Projection schema `1` has `resolver_version:2`, additive `block_ir_version:1`,
`offset_unit:"unicode-code-point"`, `complete`, and ordered block records with
`block_id`, source offsets, heading path, opaque numeric `dom_key`, and bounded
source-to-DOM segments. The initial HTML page embeds the identical map. A null
DOM key, incomplete/revision-mismatched map, or occurrence mismatch is an
explicit unmappable gap; clients fail closed rather than guessing.

Returns 400 if `id` is absent, 404 if the id is malformed or
unknown.

## `/__data/events`

```http
GET /__data/events?since=<id>&limit=200&actor=agent&concept=tables/orders&order=desc
```

Filtered read of `events.jsonl` for the change list. Defaults to
newest-first; `?order=asc` opts into oldest-first (used by
[`scripts/okf-loom watch --since`](cli.md#watch) replay).

```json
{ "events": [ /* §7.2 schema rows */ ], "rev": "<current bundle rev>" }
```

## `/__events`

SSE stream (`text/event-stream`). One event per
change/presence/activity/comment event. 15-second heartbeat
(`type=ping`). Capped at `studio.max_sse_clients` concurrent
streams (default `32`); returns 503 when full or when the studio is
absent / `--no-watch-ui` is set.

| Frame type | Carries |
|---|---|
| `ready` | Initial frame: `rev` + per-doc rev map (`doc_revs`). |
| `changed` / `created` / `removed` | A concept mutation (debounced). |
| `graph` | A graph-affecting mutation (re-fetch the graph). |
| `presence` | Agent presence state change. |
| `comment` | A comment directive was appended or transitioned. Carries `actor`, plus `parent_id` when the comment is a reply, so a tab that did not post the reply still threads it under its parent. |
| `comment_link` | A resolve back-stamped an activity event. |
| `ping` | Heartbeat. |
| `resync` | Server instructs client to refetch (rev + per-doc rev map). |

`X-Accel-Buffering: no` is sent so buffering proxies do not stall
the stream. A HEAD request to `/__events` returns an empty body
immediately (it must not enter the SSE loop).

Every persisted frame has a globally unique, strictly increasing numeric
`event_cursor`, emitted as the SSE `id`. `Last-Event-ID` resumes oldest-first;
clients deduplicate by cursor. A gap, cursor older than retained history,
`resync`, or polling takeover triggers canonical `/__comments` and document
refetch. Events notify; they never reconstruct anchor state.

Before committing SSE `200`, the server validates query/cursor shape and
performs fallible canonical-inventory plus retained-event preflight. Unknown or
duplicate query parameters follow the polling refusal contract. Preflight
failure returns unavailable instead of truncating an accepted stream; polling
resync carries the same current `rev` and `doc_revs` payload.
Intervals above 10,000 retained events return `replay_too_large` on both SSE and
polling. Ordinary polling pages are cursor-relative and oldest-first; a limited
page cannot skip the middle interval by jumping to newest events.
Replay/live handoff is subscribe-first. The retained snapshot is read after
subscription, and high-water cursor deduplication makes a concurrently published
event visible exactly once.

## `/__comments`

```http
GET /__comments?state=open&concept=tables/orders
```

Canonical comment state from `directives.jsonl` (last-write-wins
per id). The studio client consumes this directly rather than
reconstructing from the event feed. Each row carries `archived`, `summary`,
`ts`, `updated_at`, `row_token`, and a derived revision-qualified
`anchor_resolution` when the concept is available. Resolution is not persisted. See
[comment_lifecycle.md](comment_lifecycle.md).

```json
{ "comments": [ /* see comment_lifecycle.md § JSON shape */ ] }
```

This JSON route is not server-rendered into a no-JavaScript concept page.
Without the Studio client, the page shows authored concept content and one
fallback notice only—not comments, stored quotes, anchor health, highlights,
Jump, or Re-anchor controls.

## `/__feedback-import`

Local operator page for importing one portable feedback note: choose a
`.okf-feedback.json` file, review the preview, then confirm. Only the live server
serves it; `build` and `render` never emit it, and its `feedback-import.js` and
`feedback-import.css` are serve-only static assets. The markup is built in and
never replaced by a bundle template override, because the page carries the local
Studio token. See [Import a portable feedback note](/how-to/import_portable_feedback.md).

Public ingress gets `403`; the route is in no public allowlist. Any other
request gets `200`: like other GET pages, and unlike the POST routes, it checks
no token and no Origin/Host. It shows one state, chosen in
this order: no Studio attached (unavailable), `--no-edit` (read-only), then a
tunnel, public ingress, or non-loopback socket (local only); otherwise import is
available. Only the available state renders the file chooser. The page is
presentation only: preview and confirm run their own full check order, token
included.

The page sends the file's exact bytes to preview, shows the note, its unverified
claims, and the origin and confirm notices, and offers **Import as comment** only
for a `ready` preview. That offer is withdrawn (the preview goes stale until
**Preview again**) when the token nears its 10-minute lifetime, when a live
event changes the target page, on a live resync or connection change, or when
the page is restored from browser history. A confirm whose outcome is unknown is
shown as not confirmed with **Check again**, which previews the same bytes and
reports `already_imported` if the comment exists. The Studio home and command
palette link here only in a `local-edit` session.

## `/__diff`

```http
GET /__diff?concept=tables/orders&from=<rev>&to=<rev>
```

Line diff between two revs of one concept. Requires the CSRF token
(because it exposes prior snapshot bytes). Returns 400 if any of
`concept`, `from`, `to` is missing; 404 if either snapshot is
missing.

```json
{
  "ok": true,
  "concept": "tables/orders",
  "from": "<rev>",
  "to": "<rev>",
  "diff": [
    {"kind": "ctx", "num": 12, "text": "..."},
    {"kind": "del", "num": 13, "text": "..."},
    {"kind": "add", "num": 13, "text": "..."}
  ]
}
```

## `/__static/<file>`

Static assets (JS modules, CSS). Bundle overrides under
`.okf-loom/viewer/static/<file>` are served first **only** when the
active-code gate is open (bundle declares
`viewer.allow_active_code: true` AND operator consent via
`--allow-active-code` / `OKF_LOOM_ALLOW_ACTIVE_CODE`). Path traversal is
rejected (`..` and separators after normalisation; NUL bytes).

## `/<path>.<media ext>`

Bundle-local media file, so `![shot](/research/assets/shot.png)`
renders inline in the studio. Only paths whose extension is in the
media allowlist route here — everything else stays a concept path.
A media-suffixed path that names no existing file falls through to
concept routing (`shot.png.md` is a legal concept rendering at
`/shot.png`); when both exist, the file wins.

| Kind | Extensions |
|---|---|
| Images | `.png` `.jpg` `.jpeg` `.gif` `.webp` `.avif` `.bmp` `.ico` `.svg` |
| Video | `.mp4` `.webm` |
| Documents | `.pdf` |

A file is served only when **all** of these hold, otherwise `404`:

| Guard | Refuses |
|---|---|
| Clean segments | `..` / `.` / NUL / backslash anywhere in the path. |
| §5 visibility | Anything the bundle scan prunes: `.okf-loom` session state, `.git`, `node_modules`, gitignored paths. `bundle.include` revives media the same as concepts. |
| Containment | A symlinked file resolving outside the bundle root. |
| Size cap | Files over `MAX_RAW_RESPONSE_BYTES` return `413`. |

Responses carry a **sandboxed CSP** (bundle media is user content: a
bundle SVG must not run script in the studio origin when navigated
to directly; `<img>`/`<video>` embedding is unaffected) and a weak
mtime+size `ETag` — image-heavy pages revalidate with `304`s instead
of re-downloading every screenshot. Static builds copy the same
§5-visible media set into the output, so exports render identically.

# POST routes

Before reading any POST body, the server performs side-effect-free admission:
listener/public-ingress policy, method and path existence, body-independent
authorization and headers, route availability/read-only state, and the
effective route body cap. A forbidden, unauthorized, unknown, unavailable, or
oversized request receives its existing route status and envelope immediately;
the server does not wait for the advertised body. These early refusals close
the connection rather than drain attacker-controlled bytes.

All POST routes require the CSRF token + Origin/Host allow-list and all bodies
are JSON. The global fallback cap is `MAX_EVENT_BODY_BYTES` (1 MiB), while
`/__comment-reanchor` and non-create Proposal Review mutations are capped at
65,536 bytes; Proposal Review create retains its 1 MiB cap. The raw-note
`/__feedback-import/*` routes are capped at 32 KiB (`413 note_too_large`). A declared
`Content-Length` above the effective cap returns `413` immediately and closes
the connection. Chunked transfer encoding remains unsupported and invalid
lengths retain their route-specific `400`/`422` contract. Malformed JSON returns
`400`. Every mutating endpoint returns `503` when no studio session is attached.

Only an admitted request with a complete body crosses the application dispatch
and mutation fence. The transport owns one monotonic body deadline (5 seconds
by default) across all reads: expiry returns typed `408 request_body_timeout`,
while an EOF/half-close before the declared byte count returns typed
`400 incomplete_request_body`. Both close the connection and leave state
unchanged. A slow body that completes within the deadline is dispatched once.
Unsupported local `PUT`, `PATCH`, and `DELETE` requests return the existing
`501` response before body buffering. Re-anchor rate capacity is observed
without mutation during preflight and consumed atomically only after the body
is complete and parsed, immediately before route dispatch; timeout, EOF, and
shutdown therefore consume no quota, while concurrent completed requests
cannot exceed the cap.

| Body transport outcome | HTTP result | Connection | Mutation |
|---|---|---|---|
| Pre-body policy/auth/path refusal | Existing route-specific `4xx`/`5xx` envelope | Close; body is not drained | None |
| Declared length above effective route cap | `413` route-specific envelope | Close; body is not read | None |
| Complete body before deadline | Route result | Existing route keep-alive/close policy | At most one admitted dispatch |
| Deadline before declared bytes arrive | `408 request_body_timeout` | Close | None |
| EOF/half-close before declared bytes arrive | `400 incomplete_request_body` | Close | None |
| Terminal server shutdown during partial body | Socket cancelled before dispatch | Close | None |

## `/__comment`

Append a user comment or threaded reply.

The request is a closed schema: unknown and duplicate keys are rejected;
declared fields use exact types; `detail` must be an object; and malformed
values are never string-coerced or silently replaced. `Idempotency-Key` is
optional, but keyless creates/replies still receive a durable outbox identity.

```json
{
  "concept": "tables/orders",
  "body": "Add a depends_on relation to customers.",
  "detail": {"intent": "add_relation"},
  "actor": "user",
  "parent_id": "01JABED9K0"
}
```

This minimal reply example deliberately omits the optional `anchor`. When an
anchor is supplied, use either `{"kind":"concept"}` or the complete closed
v2 object in [spec §12.1](spec.md#121-annotations-v2-contract-normative-implemented);
placeholder or additional keys are rejected.

| Field | Required | Notes |
|---|---|---|
| `concept` | yes | Concept id the comment is anchored to. |
| `body` | yes | The comment text. |
| `anchor` | no | Either `{kind:"concept"}` or a closed v2 text-quote anchor from spec §12.1. A submitted v2 anchor is recaptured against the canonical concept and must match exact text, offsets, block/hash/id, heading, and maximal bounded context before persistence. Legacy text anchors remain readable but are not accepted for new creation. |
| `detail` | no | Free-form metadata (e.g. Quick Action intent). |
| `actor` | no | Public HTTP comments are user-authored: omitted or exactly `user`. `agent` is rejected; trusted agent actions use CLI workflows. |
| `parent_id` | no | Id of the parent comment for a threaded reply. Replying to an archived thread auto-unarchives the parent. |

Response `201 Created`:

```json
{ "ok": true, "comment": { /* see comment_lifecycle.md § JSON shape */ } }
```

An `Idempotency-Key` header is handled by the bounded crash-consistent create
WAL. Keys beginning with `pfn.` are reserved for portable feedback import and are
rejected with `400 {"ok":false,"error":"reserved_idempotency_key"}`. The canonical body fingerprint is part of its identity; restart/lost
response replay cannot append twice, and changed-body reuse conflicts. State is
stored under `.comment-create-wal/`; recovery proves or emits exactly one
transaction-tagged notification before marking notification delivery complete.
Normal and recovered delivery use the same full event payload. Pending delivery
is reconciled before WAL retirement, including tombstone replay.
The immutable full receipt is durable before the directive becomes visible and
strictly validates canonical field types and grammars. Lifecycle and re-anchor
events use the canonical builder and carry the newly appended `row_token`.
New lifecycle events additionally carry `operation:"lifecycle"`: `actor` names
the actual supplied mutating label (or null when unknown), and `ts` equals that
row's mutation `updated_at`, not the comment's creation time. Admitted HTTP row
updates are attributed to `user`; only trusted claim/resolve workflows supply
agent labels. Canonical comment rows still retain their original `actor`/`ts`.
Create/re-anchor receipts and previously stored events are unchanged; older
events without this discriminator must not be reinterpreted as claim/resolve
attribution. Event evidence never grants mutation authority or proves a worker
is still running.
The parent and effective concept are resolved before validation. Concept
existence, anchor recapture, and append share the canonical concept lock;
finalized replay is reconciled before current-resource lookup. The shared lock
order is concept → idempotency key → comment → directives.

## `/__comment-update`

User-facing state or archive transition. **Archive is a separate
track from lifecycle state**.

This is a closed, type-safe CAS schema. Unknown or duplicate keys are rejected,
and each request contains exactly one operation: `state`, `archived`, or `body`.
Only `open` and `dismissed` are accepted public state values.
`state:"open"` is valid only for a currently dismissed comment; claimed or
resolved comments cannot be reopened through this route with incompatible
claim/reply/activity fields retained.

```json
// Dismiss (or reopen) — state track only.
{ "id": "01JABED9K0", "expected_row_token": "r2:01K0ROW8N7Q2", "state": "dismissed" }

// Archive — archive track only; state is preserved.
{ "id": "01JABED9K0", "expected_row_token": "r2:01K0ROW8N7Q2", "archived": true }

// Body edit — amend the comment text in place (open/claimed only).
{ "id": "01JABED9K0", "expected_row_token": "r2:01K0ROW8N7Q2", "body": "Add a depends_on relation to customers AND products." }
```

Server-side rules:

| Rule | Failure |
|---|---|
| `state == "dismissed"` on a claimed comment | `409` "cannot dismiss a claimed comment". |
| `archived: true` on a reply (not a root) | `400` "archive is only available on the top-level comment of a thread". |
| `archived: true` when any comment in the thread is not `resolved` | `409` with `unresolved_ids: [...]`. |
| `body` on a `resolved`/`dismissed` comment | `409` "cannot edit a resolved/dismissed comment (reopen it first)". |
| `body` empty | `400` "body must be non-empty". |
| `archived` is not a JSON boolean | `400`; strings, numbers, arrays, objects, and null are not coerced into mutations. |

`expected_row_token` is required for dismiss/reopen, archive/unarchive, and body
edit requests. It is compared byte-for-byte with the latest row token under the
per-comment lock; a stale token returns conflict without mutation.

A `body` edit bumps `updated_at` — an agent blocked in
[`scripts/okf-loom wait`](cli.md#wait) re-receives the corrected ask —
and re-derives `request_summary` from the new text. The studio UI
shows an Edit button on user-authored, not-yet-resolved comments.

Response `200 OK`:

```json
{ "ok": true, "comment": { /* updated record */ } }
```

The agent does NOT use this endpoint for resolve; it uses
[`scripts/okf-loom comment-resolve`](cli.md#comment-resolve).
See [comment_lifecycle.md § Server-side archive rules](comment_lifecycle.md).

## Document targets

`POST /__comment` accepts exactly one target field: legacy `concept:"reference/cli"`
or new `document:"reference/cli.md"`. Canonical document paths include `.md`, no
leading slash, dot segments, escapes, query, fragment, or private path components.
`document` supports real concepts, root/directory `index.md`, and `log.md`.
Use `anchor:{"kind":"page"}` for document-wide feedback; omitted anchors remain
`{}`. Reserved documents reject concept/text anchors. Exact mapped concept anchors
retain existing v2 validation. Replies specify the same target as their parent.
Known unavailable sources return 503, missing targets 404, malformed requests 400.
Existing Idempotency-Key replay and body limits apply unchanged.

New document rows/events carry `document`; reserved rows keep `concept:""`,
concept-document rows keep the real concept id. CLI agent replies inherit this
identity. Old concept rows remain unchanged and readable. Page rows are not
text-resolved and do not permit re-anchor. `GET /__comments?document=path.md`
includes legacy concept rows for that file and new document rows; combining
`document` with `concept`, duplicate/invalid document parameters returns 400.

`GET /__data/page?document=path.md` accepts exactly that one scalar parameter and
returns `{schema:1, document, concept, document_content_rev, raw, html,
anchor:{kind:"page"}}`. `concept` is null for reserved files; the revision is a
SHA-256 of exact source bytes. It intentionally has no annotation projection.
Bad parameters return 400; unavailable/hidden/unsafe/over-cap sources return 404.
The descriptor-relative canonical reader has a 16 MiB source cap and refuses
symlink traversal. Read access does not grant write permission. Served document
HTML includes the canonical `document` in `#okf-studio-bootstrap`.

Comment-only bootstrap advertises `comment:true`, `edit:false`, `presence:false`,
`authority_mode:"public-comment"`. A shared commenter can create or reply as
`actor:"user"` only. It cannot edit even its own rows because there is no
authenticated reader ownership model. Replies may auto-unarchive their parent,
as with existing replies. Local claim/resolve remains the external agent's job.
Tunnel start flags `allow_public_edit` and `allow_public_comment` are mutually
exclusive; editing-disabled sessions reject either. Stop/start is required to
change capability. Active comment-only status/start responses add
`public_comment:true`; missing means false. Public denials are redacted into
the private bounded `public-denials.json`, never served or exported.

## `/__comment-reanchor`

This implemented Annotations v2 route replaces one comment's anchor explicitly.
It does not accept a concept id: the server loads
the canonical concept from the latest comment row under the per-comment
cross-process lock, preventing cross-concept redirection.

```http
POST /__comment-reanchor
X-OKF-Token: <session token>
Idempotency-Key: 01K0REANCHOR7Y1VJHP9C3P
Content-Type: application/json
```

```json
{
  "comment_id": "01JABED9K0",
  "expected_row_token": "r2:01K0ROW8N7Q2",
  "expected_concept_content_rev": "7a1f000000000000000000000000000000000000000000000000000000000000",
  "anchor": {
    "schema": 2,
    "kind": "text-quote",
    "exact": "customer_id",
    "prefix": "column ",
    "suffix": " references",
    "heading": {"path": ["Schema", "Columns"]},
    "block": {
      "id": "paragraph:17",
      "hash": "sha256:7a1f000000000000000000000000000000000000000000000000000000000000",
      "start": 120,
      "end": 181
    },
    "position": {"start": 127, "end": 138}
  }
}
```

The request object and every nested anchor object are closed. All four request
fields are required; unknown/duplicate keys fail closed. `comment_id` matches
`^[0-9A-HJKMNP-TV-Z]{10,26}$`; `expected_row_token` is non-empty printable
ASCII up to 128 bytes; and `expected_concept_content_rev` is exactly 64
lower-case hexadecimal characters. The exact anchor types, Unicode code-point
offsets, caps, and semantics are in [spec §12.1](spec.md#121-annotations-v2-contract-normative-implemented).
The raw body cap is 65,536 bytes and is enforced before JSON parsing.

`Idempotency-Key` is required and matches
`^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$` (8–128 ASCII bytes). It is scoped to this
endpoint. Same key plus same canonical request fingerprint replays the original
response; same key plus a different fingerprint returns
`409 idempotency_conflict`. Concurrent duplicates produce one appended row.
Missing, unsafe, whitespace, or oversized keys are rejected rather than
bypassing deduplication.

The recoverable transaction protocol runs under the same per-comment lock:

1. Atomically write and fsync an endpoint-scoped WAL record containing a new
   transaction id, key, canonical request fingerprint, comment id, expected row
   token/content revision, `state:"prepared"`, and creation/expiry times.
2. Append/rotate and fsync the full directive row, which carries the same
   transaction id and request fingerprint.
3. Atomically finalize and fsync the WAL with the resulting row token, HTTP
   status, and closed response envelope; only then send the response.
4. On replay, reconcile a prepared WAL against retained directive rows under
   the lock. A matching transaction row is finalized and replayed without an
   append. If no row exists, validation and the one append resume under the same
   transaction. Any mismatched transaction identity/fingerprint fails closed.

Crash before the WAL leaves no intent. Crash after WAL but before append resumes
the prepared transaction. Crash after append but before finalize/response finds
the transaction-bearing row and replays exactly once. During directive rotation,
recovery must inspect the atomic retained old/new set; an indeterminate rotation
returns `503 unavailable` and never assumes that no append occurred. An
abandoned prepared record with no matching row is cleaned only after expiry plus
the maximum rotation-recovery window. Finalized WAL records retain a 24-hour TTL
and the existing bounded-file cap. Expiry/cap eviction does not make an unknown
outcome safe: when retained rows cannot prove the transaction outcome, replay
returns `409 idempotency_conflict`; it must not automatically append. The client
refetches canonical state and uses a new key only for a new user intent.

The server validates request shape and caps before expensive anchor work, then
under the lock loads the latest row, checks `expected_row_token`, derives and
checks the canonical concept content revision, validates that the supplied
anchor selects `exact` in that canonical concept, copies every unknown stored
row/anchor field not owned by this replacement, replaces `anchor`, stamps a new
row token/`updated_at`, and atomically appends one full row. Re-anchor does not
change lifecycle/body/archive fields and does not enter Markdown undo history.

Success is `200 OK`:

```json
{
  "ok": true,
  "comment": {"id": "01JABED9K0", "row_token": "r2:01K0NEWROW2", "anchor": {"schema": 2}},
  "concept_content_rev": "7a1f000000000000000000000000000000000000000000000000000000000000",
  "replayed": false
}
```

Once method/path identify `/__comment-reanchor`, **all** failures—including
token, Origin, Host, rate-limit, read-only, body-cap, and availability failures—
use this route's closed envelope, overriding the legacy plain-text common
convention. `fields` is optional and maps field paths to stable machine-readable
reason strings:

```json
{"ok":false,"error":{"code":"comment_conflict","message":"The comment changed; refetch it and select again.","fields":{"expected_row_token":"stale"}}}
```

| Status | Error code | Meaning / client action |
|---|---|---|
| `403` | `forbidden` | Missing/invalid token or rejected Origin/Host. Do not reveal which guard failed. |
| `403` | `read_only` | `--no-edit`; do not retry until mode changes. |
| `429` | `rate_limited` | Authenticated route request exceeded its limit; retry only after `Retry-After`. |
| `400` | `invalid_request` | Malformed JSON, duplicate/unknown key, missing field, bad id/token/revision/key grammar. Do not retry unchanged. |
| `400` | `invalid_anchor` | Anchor schema, cap, offset, source mapping, or exact-selection validation failed. Do not mutate. |
| `404` | `comment_not_found` | Comment absent from canonical directives. |
| `409` | `comment_conflict` | Latest row token differs; refetch comment and require fresh selection before save. |
| `409` | `concept_conflict` | Canonical Markdown content hash differs; refetch document and require fresh selection. |
| `409` | `idempotency_conflict` | Key was already used with a different canonical body. Use a new key only for a new user intent. |
| `413` | `invalid_request` | Raw body exceeds 65,536 bytes. |
| `503` | `unavailable` | Session/lock/atomic persistence unavailable; safe retry uses the same key/body. |

Failure precedence is deterministic and stops at the first class: (1) route
identification, (2) token plus mandatory Host and optional Origin as one
non-disclosing `403 forbidden` gate, (3) authenticated rate limit as
`429 rate_limited`, (4) `--no-edit` as `403 read_only`, (5) pre-parse body cap, (6) JSON/header and
closed-envelope validation, (7) session availability, (8) idempotency/WAL
reconciliation, (9) comment existence, row/content conflicts, and anchor
validation. No lower-precedence error is exposed when a higher guard fails.

Resolver `invalid`, `stale`, and `ambiguous` are comment projection results,
not endpoint errors. The route additionally inherits CSRF token, Origin/Host,
path-containment, no-edit, escaped-text, and no-secret/no-payload logging rules.
Only timeout, `429`, and `503` are client-retryable, with at most three attempts,
250/500 ms exponential delay plus 0–100 ms jitter; all retries retain the same
key and byte-equivalent canonical body. A timeout never changes anchor health.

## `/__feedback-import/preview` and `/__feedback-import/confirm`

Local-operator import of one portable page-level feedback note. The contract is
[spec §12.2](spec.md#122-portable-page-level-feedback-import-local-operator-only).
The body is the raw note JSON (`Content-Type: application/json`, at most 32 KiB)
and is parsed only by the closed note parser. Confirm also sends the
`X-OKF-Preview-Token` returned by preview. Neither route is in any public
allowlist. Public ingress is refused before the body is read, in edit,
comment-only, and read-only modes.

Check order: public ingress → token/Origin/Host (`403 forbidden`) → Studio
attached (`503 unavailable`) → `--no-edit` (`403 read_only`, preview included) →
local only (`403 local_only`, including whenever a tunnel is attached) → size
(`413 note_too_large`, closes) → media type (`415 unsupported_media_type`) →
parse (`400 invalid_note`, `fields.note` = reason) → replay of `pfn.<note_id>`
(finalized → `200 replayed:true`; different content → `409 idempotency_conflict`)
→ confirm-only preview token (`409 stale_preview`) → exact on-disk byte check
under the document lock (`404 target_missing`, `409 revision_mismatch`,
`503 unavailable`) → create (`201 replayed:false`). Error bodies use the
`{ok:false,error:{code,message,fields}}` envelope.

Preview response, always `200` after parsing:

```json
{"ok": true, "status": "ready", "note": {"note_id": "…", "document": "tables/orders.md",
 "source_sha256": "…", "body": "…", "note_sha256": "…"},
 "document": "tables/orders.md", "concept": "tables/orders", "source_match": true,
 "preview_token": "…", "expires_at": "2026-09-22T10:25:00Z"}
```

`status` is `ready`, `already_imported` (with `comment`), `conflict`,
`revision_mismatch`, or `target_missing`. Only `ready` carries a token, which is
valid for 10 minutes and invalidated by a restart. Preview writes no WAL,
transaction result, receipt, or event. Confirm creates an ordinary open
`{kind:"page"}` comment with `actor:"user"` and `detail:{}`, plus the server-owned
top-level `portable_import` marker described in
[comment_lifecycle.md](comment_lifecycle.md). The marker is not included in live
comment events.

## `/__presence`

`GET /__presence` returns `{version:2,presence_rev,records,browser_session}`.
`records` is the canonical, server-timestamped public snapshot; expired rows
are omitted. `browser_session` contains a newly issued opaque `session_id` and
signed `capability`. Issuance is capped at 60 per source/minute and 512 live
issued sessions. Capabilities expire after 60 seconds and bind the session,
issue/expiry times, random nonce, and current server generation; restart
invalidates prior-server capabilities. Responses are `Cache-Control: no-store`; neither value is
embedded in static, portable, or single-file output.

Routine canonical reconciliation may send both `X-OKF-Presence-Session` and
`X-OKF-Presence-Capability`. When they identify a valid, unexpired capability
from the current server generation, GET returns the snapshot with that same
`browser_session` and does not consume issuance capacity. Missing one header,
an invalid pair, expiry, or an earlier server generation returns `403`; the
client may then perform one ordinary credential-free GET to rotate capability.

Browser writes use the closed schema below. A capability can update only its
own session. `client_seq` is monotonically increasing: duplicate/stale upserts
and leaves are successful no-ops, including after a tombstoned leave.

```json
{"action":"upsert","session_id":"…","capability":"…","client_seq":1,
 "actor":"browser","state":"watching","label":"Alex",
 "focus":"tables/orders",
 "viewport":{"schema":"graph-viewport-v1","x":0.25,"y":0.75}}
```

`leave` accepts only `action`, `session_id`, `capability`, and `client_seq`.
Coordinates are finite normalized numbers in `[0,1]` (booleans are rejected),
focus is an existing canonical concept id, and labels reject control and bidi
override/isolate characters. Browser rows expire after 15 seconds; the agent
row expires after 300 seconds. Sweeps are independent of SSE disconnects. One
server-owned sweeper runs with `watch=false` and stops on clean shutdown.
It is the sole expiry mutator: GET, capability issuance, and ordinary writes
may hide/discover expiry but never delete expired rows or emit expiry deltas.
Presence notifications use the ephemeral SSE bus and browser cursor updates
are never appended to `events.jsonl`. Notifications are bounded deltas:
`{type:"presence",presence_version:2,presence_rev,action,record}` for upsert,
or the same envelope with `session_id` and `reason` for leave. They carry no
global `event_cursor`; clients resync with canonical GET.

The authority is capped at 128 active records, 1024 tombstones, 512 issued
sessions, and 512 KiB. Tombstones live through capability expiry; capacity
returns `429` rather than evicting a live replay floor. Mutation limits are
20/session/second and 500 aggregate/second, with expiring bounded registries.
Malformed requests return `400`, invalid/expired/prior-server capabilities
`403`, rate/capacity limits `429`, and corrupt or unsafe authority `503`.
Capability-registry authorization and the requested sequence mutation share
one descriptor-pinned lock and one clock read. Persistence opens every bundle
and session directory with `O_DIRECTORY|O_NOFOLLOW`, locks relative to the
pinned session fd, and performs temp write, fsync, and atomic replacement by
dirfd; pathname substitution cannot redirect the authority or lock.
Agent sequence numbers are allocated monotonically by that same store lock;
wall-clock rollback cannot turn a fresh CLI heartbeat into a stale no-op.
The sweeper's expired-agent transition and cache publication share the Studio
cache lock, so a concurrent fresh heartbeat always receives the next sequence
and cannot be overwritten by a stale idle decision.
In-process presence lock order is publication lock → Studio cache lock →
pinned presence authority. The cache lock is released before EventBus publish
or retained sidecar append, whose existing order is sidecar lock → Studio
counter lock; this prevents inversion while the publication lock preserves
presence commit/revision order. A sweep that discovers a cross-process agent
record differing from its cache emits one bounded `upsert` correction with
`reason:"cross_process"`; unchanged records do not emit duplicates, including
when bundle watching is disabled.
Expiry deltas and cross-process corrections are published while the same
descriptor-pinned cross-process presence transaction still owns the committed
revision. EventBus publication is bounded and acquires neither cache nor
sidecar locks. Consequently a fresh external heartbeat cannot commit a higher
revision before an older idle delta is published; its later correction carries
the higher revision and is emitted once.

```json
{ "actor": "agent", "state": "editing", "focus": "tables/orders", "message": "linking 3 of 7 tables…" }
```

| Field | Allowed |
|---|---|
| `state` | `idle` / `watching` / `thinking` / `editing`. |
| `focus` | Concept id (optional). |
| `message` | Optional free-text progress line (max 200 chars; longer is truncated), rendered next to the presence chip and in the presence history. Re-POST to update it mid-pass. [`comment-claim`](cli.md#comment-claim) auto-presence carries the claim summary as the message. |
| `actor` | Defaults to `agent`. |

A `user` actor's write only takes effect when the current agent
presence is already `idle` or `watching` (so a browser boot cannot
clobber an active agent). Response `200 OK` carries `presence`
(plus `preserved: true` when an agent state was preserved).

## `/__apply`

Run a whitelisted `UpdateOp` via the studio write funnel. Never
accepts a shell/argv string — only a known op kind + args.

```json
{
  "kind": "add_link",
  "target": "tables/orders",
  "args": {
    "target_concept_id": "tables/customers",
    "label": "Customers"
  },
  "group_id": "PASS1",
  "expected_rev": "<content-hash>",
  "actor": "agent"
}
```

| Field | Notes |
|---|---|
| `kind` | A whitelisted `UpdateOp` kind (see [cli.md § update](cli.md#update)). |
| `target` | The concept id to mutate. |
| `args` | Per-kind args (validated fail-closed). |
| `group_id` | Optional; groups this write with others for one-click group undo. Must be a safe path token. |
| `expected_rev` | Optional content-hash; mismatch returns `409` (§9.3 collision guard). |
| `actor` | Defaults to `agent`. |

The block-level partial-update kinds
([cli.md § update-section](cli.md#update-section) /
[cli.md § replace-text](cli.md#replace-text)) take these args:

| Kind | Required args | Optional args |
|---|---|---|
| `update_section` | `heading` (str), `body` (str) | `mode` (`"replace"` \| `"append"`, default `"replace"`), `create_if_missing` (bool) |
| `replace_text` | `old` (str), `new` (str) | `all` (bool) |

Response `200 OK`:

```json
{ "ok": true, "applied": true, "result": { /* apply_plan result */ } }
```

Conflict responses (§9.3 / §9.4): `409` with
`{ok:false, conflict:true, concept, expected_rev, current_rev}`.

## `/__undo`

Restore a prior snapshot. Single or group.

```json
// Single
{ "concept": "tables/orders", "rev": "<content-hash>" }

// Group
{ "group_id": "PASS1" }
```

| Field | Notes |
|---|---|
| `concept` + `rev` | Restore one concept to a prior rev. Both must be safe path tokens. |
| `group_id` | Restore every member of an undo group. |

Response `200 OK`:

```json
{ "ok": true, "restored": 3 }
```

Special cases:

| Case | Response |
|---|---|
| Every target already at its snapshot rev (replay) | `409` `{ok:false, conflict:true, error:"already_undone", targets, current_state}`. |
| Every snapshot pruned by the history ring cap | `404` `{ok:false, error:"snapshots_pruned", missing}`. |
| Some targets pruned, some restored | `409` `{ok:true, restored:N, partial:true, missing}`. |

## `/__preview`

Render arbitrary in-flight markdown (DoS-capped).

```json
{ "markdown": "# Hello\n\n**bold** text" }
```

```json
{ "ok": true, "html": "<h1>Hello</h1><p><strong>bold</strong> text</p>" }
```

## `/__tunnel`

Server admin: attach/detach a cloudflared quick tunnel on the RUNNING
server, so going public does not mean restarting `serve` (losing the
session token, open SSE clients, and undo history). Driven by
[`scripts/okf-loom tunnel`](cli.md#tunnel). Exempt from `--no-edit`
kiosk mode (server admin, not a bundle write) but never from the
token + Origin/Host guard.

```json
{ "action": "start", "allow_public_edit": false }
```

`allow_public_edit` is an optional start-only boolean and defaults to `false`.
Unsupported fields, non-boolean values, and use with `stop`/`status` return
`400`. Changing this capability on an attached tunnel returns `409`; stop and
start the tunnel explicitly instead.

| Action | Response |
|---|---|
| `start` | `200` `{ok: true, url, public_edit}`; `{ok, url, public_edit, already_running: true}` when the same-capability tunnel is already up (idempotent); `409` for capability conflict/no-edit public opt-in; `502` when `cloudflared` is missing or times out. |
| `stop` | `200` `{ok: true, url: null, stopped: bool}` (idempotent). |
| `status` | `200` `{ok: true, url, public_edit}` (`url` is `null` and `public_edit` is false when no tunnel is up). |

Any other `action` returns `400`. `start` creates a fresh dedicated loopback
ingress generation, points cloudflared at that listener, and records only the
public URL/capability mode in `<session>/server.json`; it does not widen the
local/admin Host allowlist. `stop` revokes the public mutation token and ingress
generation before closing the listener. A later attach creates fresh values, so
stale tokens/listeners fail closed. `/__tunnel` itself is local/admin-only. The
tunnel process and ingress listener belong to the serve process and die with it.

# Response shapes (common conventions)

| Status | Body |
|---|---|
| `200` / `201` | JSON `{ok: true, ...}` or HTML/text. |
| `400` | Route-specific JSON or HTML validation response. Retrieval JSON uses `{ok:false,error:{code,message,fields}}`. |
| `403` | Plain text. Cross-origin guard failure or read-only kiosk. |
| `404` | Plain text `Not found` / JSON `{ok:false, error:"…"}`. |
| `409` | JSON `{ok:false, ...}` (conflict, dismiss-on-claimed, unresolved-thread archive, etc.). |
| `413` | Oversized POST/raw body may be plain text; context and negotiated JSON search use the typed retrieval envelope, while HTML search renders its typed error page. |
| `414` | Typed retrieval JSON envelope when the raw request target exceeds 64 KiB. |
| `408` | Typed `{ok:false,error:{code:"request_body_timeout",...}}`; body deadline elapsed before all declared bytes arrived; connection closes without dispatch. |
| `500` | Plain text `Internal Server Error`. Full traceback logged to stderr only (no info disclosure). |
| `502` | JSON `{ok:false, error:"…"}`. Tunnel start failed (`cloudflared` missing or timed out). |
| `503` | Plain text for legacy studio availability cases; retrieval returns typed JSON, or rendered HTML for non-JSON `/__search`, on busy/deadline failure. |

Retrieval JSON failures have one stable shape and an HTTP status matching the
error class:

```json
{"ok": false, "error": {"code": "invalid_request", "message": "...", "fields": {"limit": "out_of_range"}}}
```

# Embedding the server

```python
from http.server import ThreadingHTTPServer
from okf_loom import Bundle
from okf_loom.server import OKFWikiHandler

bundle = Bundle.load("path/to/bundle")
httpd = ThreadingHTTPServer(("127.0.0.1", 8787), OKFWikiHandler)
httpd.bundle = bundle         # attribute the handler reads
# httpd.studio = Studio(...)  # attach for the live studio (optional)
httpd.serve_forever()
```

Run it in a daemon thread inside another agent process; the bundle
can be swapped at runtime (`httpd.bundle = Bundle.load(...)`).
See [/how-to/embed_in_harness.md](/how-to/embed_in_harness.md).

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [cli.md § serve](cli.md#serve) — flags, bind, public/kiosk modes.
- [cli.md § tunnel](cli.md#tunnel) — runtime tunnel attach/detach on a live session.
- [cli.md § token](cli.md#token) — reading the CSRF token.
- [comment_lifecycle.md](comment_lifecycle.md) — comment state model and archive rules.
- [config_yaml.md](config_yaml.md) — `studio.*` keys (`allowed_hosts`, `max_sse_clients`, `theme`, …).
- [search_modes.md](search_modes.md) — `/__search` backend.
- [context_packs.md](context_packs.md) — `/__context` schema, citation, budget, and snapshot contract.
- [/tutorials/live_studio_basics.md](/tutorials/live_studio_basics.md) — first studio session.
- [/explanation/live_studio_design.md](/explanation/live_studio_design.md) — why the studio is shaped this way.
- [`/reference/spec.md`](/reference/spec.md) — current studio and route contract.
