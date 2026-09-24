---
type: Reference
title: CLI command reference
description: Every `scripts/okf-loom` subcommand — purpose, key flags, exit codes, and output formats — for okf-loom 2.0.0 and OKF 0.2.
resource: /reference/cli.md
tags: [cli, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# CLI command reference

Run commands from this checkout as `scripts/okf-loom ...`.
From a parent workspace where this checkout is named `okf-loom`, use `okf-loom/scripts/okf-loom ...`.
Do not assume an installed console script.
Bundle and output-format arguments are command-specific; check each synopsis before generating commands.

okf-loom 2.0.0 implements upstream OKF SPEC `v0.2` with legacy 0.1 compatibility.
`scripts/okf-loom --version` prints `okf-loom 2.0.0 (SPEC v0.2)`.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

# Command summary

| Command | Purpose |
|---|---|
| [`info`](#info) | Bundle summary: concept/type/tag/link counts, version, extensions. |
| [`validate`](#validate) | Upstream OKF 0.2 §11 conformance + soft warnings; profiles, `--strict`, `--fail-on-broken-links`. |
| [`graph`](#graph) | Print the link graph (text / JSON / DOT). |
| [`graph-quality`](#graph-quality) | Advisory graph usefulness report; does not affect OKF conformance. |
| [`search`](#search) | Six modes behind one `--mode` flag. |
| [`context`](#context) | Build a deterministic schema-v1 citation-ready Agent Context Pack. |
| [`discover`](#discover) | Find missing links/indexes/descriptions/relations/orphans. |
| [`plan`](#plan) | Build a reviewable, executable action plan. |
| [`capabilities`](#capabilities) | List or resolve the capability registry. |
| [`serve`](#serve) | Run the live collaborative studio (HTTP server). |
| [`watch`](#watch) | Headless change feed; no HTTP server. |
| [`wait`](#wait) | Foreground block-once for the next comment/change. |
| [`token`](#token) | Print the per-session CSRF token. |
| [`tunnel`](#tunnel) | Attach/detach a cloudflared quick tunnel on a RUNNING studio (no restart). |
| [`comment-claim`](#comment-claim) | Mark a comment `claimed` by the agent, with optional `--summary`. |
| [`comment-resolve`](#comment-resolve) | Mark a comment `resolved` with reply/activity links and optional `--summary`. |
| [`comment-reply`](#comment-reply) | Post a threaded reply under the thread root WITHOUT resolving the parent. |
| [`comment-list`](#comment-list) | List comments/directives, optionally filtered. |
| [`presence`](#presence) | Set agent presence (mirror of `POST /__presence`). |
| [`render`](#render) | Render a self-contained `viz.html`. |
| [`build`](#build) | Build a multi-file static site (spa / static / single-file). |
| [`index`](#index) | Regenerate upstream §8 `index.md` listings (marker-safe). |
| [`log`](#log) | Append to upstream §9 `log.md` history (append-only). |
| [`update`](#update) | Apply an update plan or action envelope (idempotent). |
| [`upgrade`](#upgrade) | Read-only preview/check; explicit conservative 0.1 → 0.2 apply. |
| [`bootstrap`](#bootstrap) | Scaffold an empty bundle (no config file). |
| [`import`](#import) | Import existing `.md` files into a bundle. |
| [`init`](#init) | Scaffold a bundle with `index.md`, `log.md`, `okf-loom.config.yaml`. |
| [`write-concept`](#write-concept) | Create or update a single concept file. |
| [`set-frontmatter`](#set-frontmatter) | Set one frontmatter key on a concept. |
| [`update-section`](#update-section) | Replace (or append to) ONE section of a concept body. |
| [`replace-text`](#replace-text) | Exact textual patch on a concept body (fail-closed). |
| [`link-add`](#link-add) | Add a markdown link (and optionally a typed relation). |
| [`entity-add`](#entity-add) | Add an entity to a concept's `entities:` list. |
| [`repair`](#repair) | Apply mechanical fixes (index regen + relation mirroring). |

# Common conventions

| Command shape | Commands |
|---|---|
| Positional `<bundle>` + command-specific formats | `info`, `validate`, `graph`, `search`, `discover`, `repair`; consult each synopsis. `search` accepts only `text` or `json`. |
| Positional `<bundle>` + `--format {json,markdown}` | `context` |
| Positional `<bundle>` + `--format {text,json}` | `graph-quality`, `plan`, `tunnel`, `comment-claim`, `comment-resolve`, `comment-reply`, `comment-list`, `presence`, `index`, `update` |
| Optional/no positional bundle + `--format {text,json}` | `capabilities` uses optional `--bundle`; `init` requires `--bundle`; `upgrade` accepts optional positional `<bundle>` or `--bundle` |
| Positional or bundle option, no `--format` | `serve`, `watch`, `wait`, `token`, `render`, `build`, `log` |
| Non-bundle source/destination, no `--format` | `bootstrap <dest>`, `import <src> <dest>` |
| Authoring verbs | `write-concept`, `set-frontmatter`, `update-section`, `replace-text`, `link-add`, `entity-add` use `--bundle` and support `--format {text,json}` |

| Convention | Detail |
|---|---|
| `--group-id` / `--actor` | Content mutators accept these for one-click group undo and change-list attribution when a Studio session exists. `upgrade --actor` is different: an original-producer assertion, with no Studio history. |
| `--dry-run` | Preview the change without writing (where applicable). |
| Atomic writes | Every file mutation goes through tmp + rename (AGENTS.md hard rule #8). |
| Studio detection | Mutators detect the configured studio session directory (`.okf-loom/session/` by default) and route through `Studio.save_concept` (attribution + undo + events + SSE). Without a session they take the non-studio atomic write path. |
| Exit `0` | Success (or `scripts/okf-loom wait` returned work). |
| Exit `1` | Conformance failure (`validate`), `scripts/okf-loom wait --timeout` expired with no work, or a fail-closed mutator refusal (`section_not_found`, `text_ambiguous:N`, missing link target, …). |
| Exit `2` | Strict-mode failure (`validate --strict`); `scripts/okf-loom init` on a non-empty destination; `scripts/okf-loom serve --public` on a non-TTY without `--public-ack`; `scripts/okf-loom tunnel` with no live `server.json`. |

# info

```bash
scripts/okf-loom info <bundle> [--format {text,json,md,dot}]
```

Bundle summary: concept/type/tag/link counts, declared `okf_version`,
declared `okf_extensions`, and any load warnings.
First command to run against an unfamiliar bundle.

See [/tutorials/first_bundle.md](/tutorials/first_bundle.md).

# validate

```bash
scripts/okf-loom validate <bundle> [--strict] [--profile {spec,producer,loose}]
            [--fail-on-broken-links] [--checks CHECKS]
            [--format {text,json,md,dot}]
```

| Flag | Effect |
|---|---|
| `--strict` | Treat warnings as errors (exit `2` instead of `0`). |
| `--profile spec` | Default; only `type` is always required (upstream 0.2 §11). |
| `--profile producer` | Promote `concept.missing_recommended_keys` to ERROR. |
| `--profile loose` | Downgrade `concept.missing_type` to WARNING (migration aid). |
| `--fail-on-broken-links` | Promote `link.broken` to ERROR; composes with any profile. |
| `--checks a,b` | Run a subset. Category names (`link_integrity`, `concept_required_keys`, …) or dotted codes (`link.broken`, `concept.missing_type`). |

Exit `0` ok, `1` conformance fail, `2` strict fail.
See [/how-to/validate_in_ci.md](/how-to/validate_in_ci.md).

# graph

```bash
scripts/okf-loom graph <bundle> [--format {text,json,md,dot}]
```

Print the link graph. Wikilinks (`[[…]]`) are emitted as edges with
`form="wikilink"`. `dot` output renders with Graphviz; `json` is the
machine-readable shape consumed by the viewer. JSON nodes include allowlisted
grouping metadata such as `graph_cluster` and `source_system` when present.

# graph-quality

```bash
scripts/okf-loom graph-quality <bundle> [--format {text,json}]
```

Emit an advisory report about whether the bundle is likely to be useful as a
graph. This is not `validate`: valid OKF stays valid. Findings include generic
type overuse, duplicate or near-duplicate titles, sparse optional metadata,
weak hierarchy signals, disconnected components, and orphan concepts.

# search

```bash
scripts/okf-loom search <bundle> [query] [--mode {lexical,semantic,hybrid,tag,entity,relation}]
            [--type TYPE] [--tag TAG] [--limit N] [--entity ENTITY]
            [--relation RELATION] [--source SOURCE] [--target TARGET]
            [--format {text,json}]
```

`query` is optional in `relation` mode (edge filters suffice).
`--limit` defaults to `20` and accepts `1..100`.
`--type` and `--tag` apply to every mode; `--entity` applies only to entity
mode and cannot accompany a non-empty query; relation/source/target apply only
to relation mode.
Invalid combinations fail with exit `1`; argparse option, choice, and numeric
bound failures exit `2`.
The operation is read-only.
JSON preserves the established result fields and additively includes bounded
`backends`, `reasons`, and exact passage `evidence` where available.
Full mode-by-mode detail lives in [search_modes.md](/reference/search_modes.md).

# context

```bash
scripts/okf-loom context <bundle> [query]
            [--mode {lexical,semantic,hybrid,tag,entity,relation}]
            [--budget-tokens N] [--neighbors N] [--limit N]
            [--type TYPE] [--tag TAG] [--entity ENTITY]
            [--relation RELATION] [--source SOURCE] [--target TARGET]
            [--concept CONCEPT] [--format {json,markdown}]
```

Build a deterministic context pack from a query or canonical concept seed.
Defaults are mode `hybrid`, budget `6000`, neighbors `1`, limit `20`, and
format `markdown`.
Bounds are `128..50000` estimated tokens, `0..2` graph hops, and `1..100`
candidate items.
The search filters have the same mode applicability as [`search`](#search).

```bash
scripts/okf-loom context docs-bundle "comment lifecycle"
scripts/okf-loom context docs-bundle --concept reference/http_routes --format json
scripts/okf-loom context docs-bundle --mode relation --relation references \
  --neighbors 0 --budget-tokens 2000 --limit 10
```

JSON is the complete schema-v1 object; Markdown is its canonical
`representations.markdown` serialization over the same items.
The command never writes the bundle and emits no partial successful payload on
a contract error.
Argparse validation exits `2`; runtime/contract refusals follow the existing
search convention and exit `1`.
See [context_packs.md](/reference/context_packs.md) for schema, citation,
budget, count, truncation, and snapshot details.

# discover

```bash
scripts/okf-loom discover <bundle> [--rules RULES] [--out OUT]
            [--scope SCOPE] [--neighbors] [--min-confidence N]
            [--include-low-confidence] [--format {text,json,md,dot}]
```

Emit a structured report of gaps the managing agent may fix.
Suggestions carry a rule, severity, message, target concept, and an
`action` verb phrase. JSON output includes suppressed suggestions and
`suppressed_reason_counts` so filtered data remains inspectable. It also
includes `actionability_counts` and an `actionability` object that groups
items into buckets such as `safe_to_apply`, `needs_review`,
`suppressed_existing_relation`, `suppressed_generic_label`,
`suppressed_cross_cluster`, and `low_confidence`.

| Flag | Effect |
|---|---|
| `--rules a,b` | Subset of rules: `unlinked_mentions`, `missing_indexes`, `missing_descriptions`, `broken_links`, `missing_relations_hint`, `orphan_concepts`. |
| `--out plan.json` | Write the JSON report to this path. |
| `--scope ID,ID` | Restrict rules to named concept ids (current spec §7). |
| `--neighbors` | Expand `--scope` to 1-hop graph neighbours (in + out edges). |
| `--min-confidence N` | Minimum confidence for unlinked-mention suggestions (default `0.5`). |
| `--include-low-confidence` | Keep low-confidence suggestions in the main list for broad audits. |

`unlinked_mentions` suppresses suggestions already covered by structured
`relations:` metadata (`already_structurally_related`) and lowers confidence
for generic labels such as `Architecture`, `README`, or `Implementation Plan`
unless source and target share strong context such as `graph_cluster` or
folder. Mention details include `location_counts` and `occurrence_locations`
with locations `frontmatter`, `h1`, `heading`, `table`, and `body`; matches
seen only in frontmatter, headings, or tables are lower confidence than body
prose.
Bundle-specific editorial suppressions live in `okf-loom.config.yaml` under
`discover.suppress_phrases` and `discover.suppress_pairs`; configured
suppression reasons are emitted as `configured_phrase` and `configured_pair`.

The agent applies fixes directly via the mutators; the reviewable-plan
workflow (`discover` → `plan` → `update`) is still available.
See [/how-to/discover_and_fix_gaps.md](/how-to/discover_and_fix_gaps.md).

# plan

```bash
scripts/okf-loom plan <bundle> [--rules RULES] [--out OUT]
            [--scope SCOPE] [--neighbors] [--portable] [--format {text,json}]
```

Build a reviewable, executable action plan from discovery + index
staleness + typed-relation mirroring.

| Flag | Effect |
|---|---|
| `--portable` | Emit bundle-relative argv (`--bundle .`) so the plan is portable across machines. Replay from the bundle directory. |
| `--scope ID,ID` / `--neighbors` | Scope the plan (fast per-change enrichment). |

Mechanical actions carry a runnable `argv`; agent-decision actions
carry an `argv_template` with `<PLACEHOLDER>` slots, a `confidence`
score, and an `agent_instruction`.

# capabilities

```bash
scripts/okf-loom capabilities [--bundle BUNDLE] [--format {text,json}]
```

Without `--bundle`, list every capability okf-loom knows.
With `--bundle`, resolve which are active for that bundle (declared
under `okf_extensions:` plus auto-activated by observed keys).
See [capabilities.md](/reference/capabilities.md).

# serve

`--allow-tunnel-comment` is a separate opt-in with `--tunnel`: URL holders may
create comments/replies only, not change content or administer the session.
It excludes `--allow-tunnel-edit` and `--no-edit`; disabled Studio editing also
rejects it. Public read-only remains the default.

```bash
scripts/okf-loom serve <bundle> [--host HOST] [--port PORT] [--no-watch] [--no-open]
            [--allow-active-code] [--no-edit] [--tunnel] [--allow-tunnel-edit]
            [--public] [--public-ack] [--no-watch-ui]
```

Run the live collaborative studio. Default bind is `127.0.0.1:8787`.

| Flag | Effect |
|---|---|
| `--host HOST` | Bind host. A non-loopback host implies `--public` semantics. |
| `--port PORT` | Bind port (default `8787`). |
| `--no-watch` | Disable the file watcher (no live reload on disk change). |
| `--no-open` | Do not open a browser window on start. |
| `--allow-active-code` | Operator consent enabling viewer overrides + plugins for bundles that declare `viewer.allow_active_code: true`. Default defers to `OKF_LOOM_ALLOW_ACTIVE_CODE`. |
| `--no-edit` | Read-only kiosk: live reads stay on and ordinary mutation POSTs are disabled. Authenticated local `/__tunnel` administration remains available for read-only sharing; public-edit start is rejected (current spec §9 and §14). |
| `--tunnel` | Also start a Cloudflare quick tunnel (requires `cloudflared` on `PATH`) and print a public `https://…trycloudflare.com` URL. Public access is read-only by default; local editing is unchanged. |
| `--allow-tunnel-edit` | Dangerous explicit opt-in used with `--tunnel`: anyone with the public URL receives a scoped token for comment/content mutation routes. It never grants tunnel/proposal/admin/session-control authority. Rejected without `--tunnel`, with `--no-edit`, or when Studio configuration disables editing. |
| `--public` | Bind `0.0.0.0`. Requires `--public-ack` or interactive y/N ack; on a non-TTY without ack, exit `2`. |
| `--public-ack` | Non-interactive acknowledgement of the trust-boundary change implied by `--public`. |
| `--no-watch-ui` | Disable SSE live push server-side (`/__events` returns 503). |

Writes `<bundle>/<studio.session_dir>/.token` (`.okf-loom/session/.token` by default, mode `0600`) on boot.
Also writes `<session>/server.json` (`{host, port, pid, started, url, tunnel_url, tunnel_public_edit}`) at
startup — refreshed on tunnel attach/detach, removed on clean shutdown —
which [`tunnel`](#tunnel) reads to find the running server.
See [http_routes.md](/reference/http_routes.md),
[comment_lifecycle.md](/reference/comment_lifecycle.md),
[/tutorials/live_studio_basics.md](/tutorials/live_studio_basics.md),
and [/explanation/live_studio_design.md](/explanation/live_studio_design.md).

# watch

```bash
scripts/okf-loom watch <bundle> [--emit {jsonl,text}] [--since SINCE]
            [--auto-repair] [--debounce-ms DEBOUNCE_MS]
```

Headless change feed for the agent loop. Prints one event per line;
no HTTP server. Reuses the same `events.jsonl` append feed `serve` uses.

| Flag | Effect |
|---|---|
| `--emit jsonl` | Default; the §7.2 event schema, one JSON object per line. |
| `--emit text` | Human-readable one-line summaries. |
| `--since <id>` | Replay from `events.jsonl` after this id, then tail live (reconnect-safe). |
| `--auto-repair` | Run mechanical `scripts/okf-loom repair` (index regen + relation mirroring) on change, debounced. |
| `--debounce-ms N` | Coalesce auto-repair runs by this many ms (default `600`). |

# wait

```bash
scripts/okf-loom wait <bundle> [--for {comment,change} ...] [--since SINCE]
            [--timeout TIMEOUT] [--interval INTERVAL]
```

Foreground block-once primitive (current spec §12). Blocks until a new OPEN
comment and/or change event arrives, prints it as JSON, exits.
Run this in the agent's foreground; never background it.

| Flag | Effect |
|---|---|
| `--for comment` | Wait for a new OPEN user comment (default). |
| `--for change` | Wait for a new `changed`/`created`/`removed` event (proactive enrichment). |
| `--for comment change` | Whichever arrives first. |
| `--since <id>` | Skip the backlog; wait only for items whose id sorts after this. |
| `--timeout <s>` | Max seconds to wait; exit `1` on timeout with no work. Default: block indefinitely. |
| `--interval <s>` | Poll interval (default `0.5`). |

Without `--since`, the first call drains the oldest pre-existing open
comment; subsequent calls drain the rest, then settle into wait-for-new.

A returned comment carries a `queue: {pending: N, ids: [...]}` field —
the other open comments still pending — so the agent sees backlog depth
without diffing `comment-list` between loop turns. The one-work-item
contract (block once, print one item, exit) is unchanged.

# token

```bash
scripts/okf-loom token <bundle>
```

Print the per-session CSRF token the studio wrote to the configured session
directory (`<bundle>/.okf-loom/session/.token` by default, mode `0600`). Use only when POSTing
to `/__apply` / `/__undo` / `/__presence` / `/__comment` over HTTP
directly; the CLI mutators attach the token automatically.
Exits non-zero with a clear message if no session exists.

# tunnel

`tunnel <bundle> --allow-tunnel-comment` attaches a create/reply-only share.
The flag is start-only and excludes `--allow-tunnel-edit`; change capability
with `--stop` then a fresh start. Active comment-only JSON responses include
`public_comment:true` (absent means false). The local token is never exposed.

```bash
scripts/okf-loom tunnel <bundle> [--allow-tunnel-edit] [--stop] [--status]
            [--format {text,json}]
```

Attach a cloudflared quick tunnel to an ALREADY RUNNING studio session
— no restart, no lost session token, no dropped SSE clients. Reads the
live server's address from `<session>/server.json` plus the session
`.token`, then drives the token-guarded `POST /__tunnel` admin route
(see [http_routes.md](/reference/http_routes.md)). Startup-time
alternative: [`serve --tunnel`](#serve).

| Flag | Effect |
|---|---|
| `--stop` | Detach the running tunnel and revoke/close its public ingress generation and scoped token. |
| `--status` | Print the current tunnel URL (or none) and exit. |
| `--allow-tunnel-edit` | Dangerous start-only opt-in: anyone with the URL may comment and mutate. Rejected with `--stop` or `--status`. |

On start, cloudflared targets a dedicated generation-bound loopback ingress;
the local/admin `allowed_hosts` is not widened. Public requests are read-only by
default: unsafe methods reject them, public HTML contains no mutation token, and
the UI receives `edit: false`. Local studio editing/admin remains available.
The tunnel process and ingress listener belong to the serve process and die with
it. `--allow-tunnel-edit` intentionally publishes a separate scoped token for
comment/content mutation routes only; URL holders never receive the local admin
token and cannot control tunnels, proposals, preview/presence, or sensitive
admin/session routes. Stop/start rotates both ingress generation and token. A
server-owned monitor detects unexpected cloudflared exit, stops new public
mutation admission, drains already-admitted mutations, revokes the old ingress
generation and token, and permits a fresh start. Process shutdown uses bounded
terminate/wait/kill/reap handling; a stale monitor observation cannot revoke a
newer generation.

While an already-admitted public mutation drains, status remains responsive and
reports no active URL with `draining: true`; a new start fails promptly with a
retryable conflict instead of overlapping the old generation. If the owned
cloudflared process cannot yet be proven reaped, public authority remains
revoked while the server retains settlement ownership. Start stays unavailable,
and a later stop or server shutdown retries bounded settlement.
The same ownership rule applies when cloudflared starts but never prints a URL:
the provisional ingress is revoked, no URL is published, and an unproven
process remains settlement-pending rather than being orphaned or overlapped.

An ordinary stop or detected tunnel death continues waiting for every admitted
durable mutation. Final `serve` shutdown is the terminal exception: it closes
admission and the public listener, waits a bounded drain deadline, then settles
cloudflared and continues process shutdown with URL, token, and generation
inactive. Python request threads are not cancelled; process exit is the final
abort owner, and the terminal-closing lifecycle never permits a replacement
tunnel to start.

No `<session>/server.json` (no live server): exit `2`. Connection
refused (stale session state; server crashed): diagnostic + exit `1`.
`cloudflared` missing or slow to come up: the server answers `502` and
the CLI exits `1`.

# comment-claim

```bash
scripts/okf-loom comment-claim <bundle> <comment_id> [--actor ACTOR]
            [--summary SUMMARY] [--no-presence] [--format {text,json}]
```

Mark a comment `claimed` by the agent. Sets `claimed_by`; auto-flips
presence to `editing <concept>` unless `--no-presence` is given. The
auto-presence carries the claim summary as the presence `message`, so
the user sees WHAT is being edited, not just that editing happens.

| Flag | Effect |
|---|---|
| `--actor ACTOR` | Who is claiming (default `agent`). |
| `--summary SUMMARY` | Short agent-authored blurb shown in the collapsed thread preview. |
| `--no-presence` | Skip the automatic presence flip to `editing`. |

Canonical loop: `scripts/okf-loom wait` → `scripts/okf-loom comment-claim` → mutators →
`scripts/okf-loom comment-resolve`.

# comment-resolve

```bash
scripts/okf-loom comment-resolve <bundle> <comment_id> [--reply REPLY] [--summary SUMMARY]
            [--activity ACTIVITY] [--actor ACTOR] [--format {text,json}]
```

Mark a comment `resolved` with an optional reply and activity links.

| Flag | Effect |
|---|---|
| `--reply REPLY` | Long-form resolution message shown in the change list. |
| `--summary SUMMARY` | Short agent-authored blurb shown in the collapsed preview (separate from `--reply`). |
| `--activity ID,ID` | Comma-separated change-list entry ids the resolution produced (drives one-click group undo). |
| `--actor ACTOR` | Who is resolving (default `agent`). |

# comment-reply

```bash
scripts/okf-loom comment-reply <bundle> <comment_id> [--body BODY] [--body-file BODY_FILE]
            [--actor {user,agent}] [--format {text,json}]
```

Post a threaded reply WITHOUT resolving the comment — the
clarifying-question channel for an ambiguous ask. `--body` (or
`--body-file`) is required and must be non-empty.

| Flag | Effect |
|---|---|
| `--body BODY` | Reply text shown in the thread. |
| `--body-file FILE` | Read the reply text from this file. |
| `--actor {user,agent}` | Reply author role persisted on the created receipt (default `agent`). A created receipt's actor is this closed role; other values — including named identities such as `aic/Polaris` — are rejected with a usage error before any directive is created. Named identities belong to [`comment-claim`](#comment-claim)/[`comment-resolve`](#comment-resolve) `--actor` attribution. |

The reply lands under the thread **root** (replying to a reply hoists
to the root, matching the 2-level display cap) and never touches the
parent's lifecycle state. It is created with `state=resolved` and
`claimed_by=<actor>` (a statement, not an ask), so
[`wait --for comment`](#wait) never returns the agent its own reply
and thread archive (all-resolved) stays satisfiable. Unknown comment
id: exit `1`. The user's answer — their reply — wakes `wait` as new
work.

# comment-list

`--document path.md` filters new page targets and legacy concept rows for that
file, including indexes/logs. It excludes `--concept`. Text output shows the
document path when present; JSON retains the stored row and unknown fields.

```bash
scripts/okf-loom comment-list <bundle> [--state {open,claimed,resolved,dismissed,archived}]
            [--concept CONCEPT] [--format {text,json}]
```

List directives from the configured session directory (`<bundle>/.okf-loom/session/directives.jsonl` by default),
optionally filtered. Each row carries `archived: bool` (separate
track), `summary: str|null`, `ts` (immutable creation time), and
`updated_at` (bumped on every transition).
See [comment_lifecycle.md](/reference/comment_lifecycle.md) and
[/how-to/archive_threads.md](/how-to/archive_threads.md).

# presence

```bash
scripts/okf-loom presence <bundle> [--state STATE] [--focus FOCUS] [--message MESSAGE]
            [--actor ACTOR] [--format {text,json}]
```

CLI mirror of `POST /__presence`.

| Flag | Effect |
|---|---|
| `--state STATE` | One of `idle`, `watching`, `thinking`, `editing` (default `idle`). |
| `--focus FOCUS` | Concept id the agent is focused on. |
| `--message MESSAGE` | Free-text progress line (max 200 chars; longer is truncated) rendered next to the presence chip and in the presence history. Re-post to update it mid-pass (`"linking 3 of 7 tables…"`). |
| `--actor ACTOR` | Who is setting presence (default `agent`). |

Stale presence is auto-recovered: a watcher sweep reverts to `idle`
after `studio.presence_ttl_seconds` (default `300`).

# render

```bash
scripts/okf-loom render <bundle> [--out OUT] [--name NAME] [--allow-active-code]
            [--include-comment-summary] [--include-portable-feedback]
            [--assets {cdn,offline}]
```

Render one self-contained `viz.html` (Cytoscape.js + stdlib markdown
renderer; embeds the bundle as JSON). Suitable for `file://`,
email, or attaching to a ticket.
`--include-portable-feedback` adds the offline feedback note composer described
under `build` below and prints `Portable feedback: N pages ready, M skipped`.
`--assets offline` embeds verified vendored renderer libraries instead of CDN
references; see `--assets` under `build` below.

# build

```bash
scripts/okf-loom build <bundle> [--target {spa,static,single-file}] [--out OUT]
            [--name NAME] [--allow-active-code] [--include-comment-summary]
            [--include-portable-feedback]
            [--output-policy {replace,managed}] [--adopt-output]
            [--assets {cdn,offline}]
```

| Target | Output |
|---|---|
| `spa` | Quartz-style default for published reading. |
| `static` | Server-rendered, JS-optional, printable, SEO/airgapped-friendly. |
| `single-file` | One portable HTML with everything embedded (same shape as `render`). |

`--allow-active-code` is the operator-consent gate for build-time
viewer overrides + plugins.
`--include-comment-summary` is valid for static and single-file output and
embeds only sanitized active-comment counts from a locked V2 session snapshot.
It is off by default; corrupt or unavailable comment authority fails closed.
The resulting portable artifact embeds the snapshot and performs no comment
network requests.
`--include-portable-feedback` is valid for static and single-file output; with
`--target spa` the command exits `2` before loading the bundle. It is off by
default, and without it the output is byte-for-byte unchanged. With it, every
concept page gets a **Write feedback note** button that opens an offline
composer. The reader writes one page-level note and downloads it as
`<concept-slug>-<note-id-prefix>.okf-feedback.json`, or copies the JSON. Nothing
is sent anywhere and the draft stays in memory only. Each note targets the
SHA-256 of the page's exact on-disk source bytes. A page whose source changed,
disappeared, could not be read safely, or is not UTF-8 after the bundle was
loaded gets no composer: the build prints
`warning: portable feedback skipped for <document>: <reason>` and still
succeeds. The build output reports `portable_feedback_pages` and
`portable_feedback_skipped`. Reserved `index.md`/`log.md` pages never get a
composer. See the [specification](/reference/spec.md) §9 and §12.2.
When authoritative event history exists, static and single-file targets also
embed a bounded recent-change snapshot by default. This is independent of the
comment-summary flag, read-only, and omitted when no authority exists.

`--assets` (all targets, and `render`) chooses where the Mermaid, KaTeX,
highlight.js, and Cytoscape graph libraries come from:

- `cdn` (default) loads the pinned versions from `cdn.jsdelivr.net`. Its output
  is unchanged except that `renderers.js` (and `single-file`/`render` HTML,
  which inlines it) carries the dormant offline loader.
- `offline` ships byte-verified copies of the same versions from
  `scripts/okf_loom/viewer/vendor/`, so the output makes no third-party network
  requests. `static` and `spa` write them under `__static/vendor/` together
  with each library's `LICENSE`, `THIRD-PARTY-NOTICES.txt`, and
  `BUNDLED-DEPENDENCY-NOTICES.txt` (the full licence texts of the third-party
  code bundled inside those libraries). `single-file` and `render` embed them,
  along with both notices files and each library's licence text, in the HTML.
  Every page gets
  a CSP without the CDN. There is no CDN fallback: a missing local asset leaves
  the authored source visible with the renderer error
  `offline-asset-unavailable`. Expect about 6.6 MB of extra static/SPA output,
  and up to about 7.1 MB for a single file. A single offline file works from
  `file://`. A `static` site loads its libraries from `file://`, but its graph
  and search fetch data and need HTTP. `spa` uses root-absolute `/__static`
  paths and always needs an HTTP server serving the site root.

| Exit | Condition (`--assets offline`) |
|---|---|
| `2` | Combined with `--allow-active-code`: refused before loading, nothing written. |
| `2` | The effective active-code gate is open (`OKF_LOOM_ALLOW_ACTIVE_CODE` plus the bundle's `viewer.allow_active_code`): `no files changed`. |
| `1` | `error: offline asset integrity check failed: <reason>; no files changed`: a vendored file or locked notice file is missing, modified, unlocked, or a symlink; the vendor folder itself is a symlink or not a directory; or a file does not match the lock or the code's SRI pins. Checked before any write. |

See the [specification](/reference/spec.md) §9.

`--output-policy` controls ownership of the output directory:

- `replace` (default, unchanged) replaces a `static`/`spa` output directory
  wholesale, including a nonempty one, and writes no manifest. `single-file`
  replaces only `index.html` and keeps sibling files.
- `managed` (`static` and `spa` only) also publishes a `.okf-export.json`
  manifest listing every file and directory it wrote. A later managed build
  replaces the directory only when everything in it is listed and
  byte-identical. A missing, empty, or unchanged managed directory is accepted.
  Unknown, edited, or deleted files, symlinks, and special files are refused.
- `--adopt-output` (requires `managed`) accepts a nonempty directory without a
  manifest only when it is byte-identical to this build (for example, an
  unmodified earlier default build of the same bundle). It never overrides a
  present manifest.

Switching `static` and `spa` over one managed output is allowed, and
`--include-portable-feedback` works with managed static output. Managed runs
add `output_policy` and `ownership` (`created`, `replaced_managed`, or
`adopted`) to the printed stats. Every refusal leaves the destination
byte-identical and prints
`error: export.ownership.<code>: <out> (<n> finding(s); first: '<path>'); no files changed`.

| Exit | Condition |
|---|---|
| `2` | `--adopt-output` without `--output-policy managed`; `--output-policy managed` with `--target single-file`; an invalid `--output-policy` value. All are refused before loading, with nothing written. |
| `2` | `export.ownership.unmanaged_nonempty`: nonempty output without a manifest, and no `--adopt-output`. |
| `2` | `export.ownership.adopt_mismatch`: `--adopt-output`, but the tree differs from the fresh build. |
| `2` | `export.ownership.manifest_malformed`: the manifest is non-canonical, invalid, oversized, or a directory. |
| `2` | `export.ownership.manifest_unsupported`: the manifest `version` is not the integer `1`. |
| `2` | `export.ownership.entry_unknown`: a file or directory is not in the manifest. |
| `2` | `export.ownership.entry_modified`: a listed entry is missing, or its kind, size, or bytes changed. |
| `2` | `export.ownership.entry_symlink` / `export.ownership.entry_special`: a symlink, FIFO, socket, or device is present. |
| `1` | A symlinked or non-directory output path, source overlap, or a publication recovery error (unchanged); running out of file descriptors or memory while inspecting the output. |

Adoption needs a byte-identical rebuild. Clock-dependent freshness badges
(`stale_after`) or a changed recent-change snapshot make it refuse with
`adopt_mismatch`. See the [specification](/reference/spec.md) §9.
See [/how-to/build_static_site.md](/how-to/build_static_site.md).

# index

```bash
scripts/okf-loom index <bundle> [--dry-run] [--frozen] [--emit-json] [--format {text,json}]
```

Regenerate upstream OKF 0.2 §8 `index.md` files (marker-safe).

| Flag | Effect |
|---|---|
| `--dry-run` | Return a plan: `would_write`, `would_skip_hand_authored`, `would_skip_unchanged`. |
| `--frozen` | Refuse a selected hand-authored index before any index writes; managed indexes may still be written. Not a dry-run. |
| `--emit-json` | Write portable `content.json` + `graph.json` under `<bundle>/.okf-loom/index/`. |

Safety: hand-authored `index.md` (no `generated: true` marker and no
`<!-- okf:generated:index begin/end -->` markers) is never overwritten.
Paired markers preserve surrounding body prose. A root index with
`generated: true` but no markers has its whole body regenerated while
preserving frontmatter. Only the root index may carry frontmatter.

# log

```bash
scripts/okf-loom log <bundle> --append "…" [--kind KIND] [--date DATE]
            [--log-path LOG_PATH] [--dry-run]
```

Append to upstream OKF 0.2 §9 `log.md` (always append-only).

| Flag | Effect |
|---|---|
| `--append TEXT` | Entry text to append under today's date. |
| `--kind KIND` | Bold-tag prefix: `Update`, `Creation`, `Deprecation`, `Initialization`. |
| `--date DATE` | Override the date heading (YYYY-MM-DD; default today). |
| `--log-path PATH` | Path to `log.md` relative to the bundle. |

# update

```bash
scripts/okf-loom update <bundle> --plan PLAN [--dry-run] [--group-id GROUP_ID]
            [--actor ACTOR] [--format {text,json}]
```

Apply an update plan or action envelope (idempotent). Accepts both
`{"ops":[...]}` (the original plan shape) and `{"actions":[...]}`
(the envelope emitted by `plan`). Template actions with unfilled
`<...>` slots are skipped with reason `needs_agent_input`.

# upgrade

```bash
scripts/okf-loom upgrade [<bundle> | --bundle BUNDLE_FLAG] [--preview | --check | --apply]
    [--to-spec TO_SPEC] [--actor ACTOR] [--expected-revision REVISION] [--format {text,json}]
```

No-argument `upgrade` reports toolkit versions. With a bundle, the default is
read-only preview; `--preview` and `--check` also write nothing, including locks
and runtime directories. Default/only supported target is `0.2`.

For a valid but unsupported source version, plain `upgrade <bundle>` without
mode/target/actor options retains informational exit `0` (`status: unsupported`
in JSON, no plan). Explicit `--preview`, `--check`, `--apply`, `--to-spec`, or
`--actor` requests support checking and rejects that source instead.

- `--apply` explicitly opts into migration from declared `0.1`; declared `0.2`
  can finish deferred additions. Undeclared bundles are assumed current and left
  undeclared, not silently adopted. Unsupported conversions fail closed.
- Without `--actor`, legacy timestamps remain untouched. With it, valid aware
  timestamps lacking any `generated` key get additive generation metadata;
  `--actor` asserts the original producer, not the migration operator. Existing
  generation, timestamps, unknown fields, citations, and prose are preserved.
- Root declaration is written last, using Linux descriptor-relative atomic
  **per-file exchange with verification**, not conditional CAS or one bundle
  transaction. Unsupported exchange fails closed. Caught failures reconcile
  actual displaced inodes/bytes; uncertainty preserves remaining recovery
  evidence and returns `upgrade.rollback_uncertain` or `upgrade.cleanup_uncertain`.
  After interruption, inspect sources and all `.okf-upgrade-*/entry` evidence
  before a fresh preview/rerun with the same actor. See the
  [migration recovery limits](/how-to/migrate_spec_version.md#interruption-and-concurrency).
- `--expected-revision` is optional and valid only with apply. It binds the
  source/config snapshot to the preview revision, not the actor/options. Apply
  always rechecks its plan and target identities/bytes, and verifies displaced
  source authority after exchange. It does not certify absence of all
  noncooperating writes or provide universal automatic crash recovery.
- Exit `0` includes `partial` reports. Inspect JSON `status` (`ready`, `partial`,
  `noop`), `revision`, `changes`, `deferred`, and `applied`; success is not
  conformance certification. Unsupported input/conflicts/failures exit `1`.
- Target syntax is ASCII `major.minor` (surrounding whitespace and leading zeros
  accepted). Malformed arguments, incompatible mode flags, missing required
  bundle, both bundle forms, or revision without apply are usage errors (`2`).

No indexes, log entries, or Studio undo history are generated by migration.
Version declarations remain optional; empty/null declarations are malformed,
not absent. See the [version contract](/reference/spec.md#format-versions-and-upgrade-diagnostics).

See [/how-to/migrate_spec_version.md](/how-to/migrate_spec_version.md).

# bootstrap

```bash
scripts/okf-loom bootstrap <dest> [--name NAME]
```

Scaffold an empty bundle at `<dest>`. Creates the directory, a
minimal root `index.md`, and an append-only `log.md`. Does NOT write `okf-loom.config.yaml`
(use [`init`](#init) for that).
New scaffolds declare OKF `0.2`.

# import

```bash
scripts/okf-loom import <src> <dest> [--default-type DEFAULT_TYPE] [--overwrite]
```

Import existing `.md` files from `<src>` into a new or existing bundle at `<dest>`,
injecting `type:` frontmatter where missing or empty. Prefer a separate destination
so the original tree remains available for review and rollback.

| Flag | Effect |
|---|---|
| `--default-type TYPE` | Default concept type for files without a type guess (default `Reference`). |
| `--overwrite` | Overwrite existing destination files (default: skip). |

Type precedence is: existing non-empty source `type`, filename hint, first-heading
hint, then `--default-type` (or `Reference`). The fallback is not a forced override.
Documents already carrying a non-empty type are copied byte-for-byte, including
comments, quotes, unknown keys, body whitespace, line endings, and final-newline
presence. When adding a type, import uses the existing round-trip frontmatter
writer and retains the exact body and LF/CRLF style; complex header edits may
fall back to YAML serialization. A missing/empty title is still derived from the
filename in that case. Import does not invent source authors, timestamps, or
provenance.

Reserved `index.md`/`log.md`, symlinked source files, and existing destination
files without `--overwrite` are intentional skips. Source discovery follows the
usual bundle exclusion rules. Retrying without overwrite leaves existing files
unchanged. Publications remain atomic and serialized under the bundle lock;
destination paths resolving outside the bundle are refused.

Per-file read, parse, metadata, and write failures are reported separately on
stderr with a source-relative path, stage, and recovery guidance. Other valid
files continue importing. Diagnostics omit exception text and source/config
contents, but filenames themselves are visible. The best-effort CLI retains its
zero exit status after a completed batch, even with per-file failures; inspect
the diagnostics and validate the resulting bundle before relying on it.
The Python `import_directory` result retains `imported`, `skipped`, `bootstrapped`,
`guessed_types`, and `dest`, and adds an `errors` list of `{path, stage, message}`
records. `skipped` now counts intentional skips only, and `guessed_types` lists
only successfully imported files. Fix failed sources or destination conditions
and retry; successfully imported peers will be skipped by default.

# init

```bash
scripts/okf-loom init --bundle BUNDLE [--name NAME] [--format {text,json}]
```

Scaffold a new bundle at `<bundle>` (must not exist or be empty; exit
`2` otherwise). Writes `index.md`, `log.md`, AND a commented-defaults
`okf-loom.config.yaml`.

# write-concept

```bash
scripts/okf-loom write-concept --bundle BUNDLE --id ID --type TYPE
            [--title TITLE] [--description DESCRIPTION] [--resource RESOURCE]
            [--timestamp TIMESTAMP] [--tag TAG] [--body BODY] [--body-file BODY_FILE]
            [--force] [--no-defaults] [--format {text,json}]
            [--group-id GROUP_ID] [--actor ACTOR]
```

Create or update a single concept (current spec §7). `--id` is the concept
id (e.g. `tables/orders`); the file path is derived. `--type` is
required. Updating merges frontmatter (only the passed keys are
touched; unknown keys are preserved). The body is replaced only if
`--body`/`--body-file` is given AND the existing body is empty;
otherwise `--force` is required to overwrite prose.

On CREATE, `resource` defaults to the bundle-absolute concept path (e.g.
`/topics/x.md`) and `generated` records `by: okf-loom/2.0.0` and `at: <now UTC>`.
Explicit `--resource` wins; explicit `--timestamp` preserves legacy input and
suppresses generation inference. UPDATE never injects defaults; `--no-defaults`
opts out. The JSON result carries `defaults_applied: ["resource", "generated"]`
when both defaults fired. Content keys (title, description, tags) are never
invented; provide them too for a warning-free strict run.

Refuses reserved filenames (`index.md`, `log.md`) case-insensitively.
See [/how-to/author_with_verbs.md](/how-to/author_with_verbs.md) and
[frontmatter.md](/reference/frontmatter.md).

# set-frontmatter

```bash
scripts/okf-loom set-frontmatter --bundle BUNDLE --id ID --key KEY --value VALUE
            [--json-value] [--dry-run] [--format {text,json}]
            [--group-id GROUP_ID] [--actor ACTOR]
```

Set one frontmatter key on a concept. `--json-value` parses the value
as JSON (lists/objects). Thin wrapper over the `set_frontmatter`
update op.

# update-section

```bash
scripts/okf-loom update-section --bundle BUNDLE --id ID --heading HEADING
            [--body BODY] [--body-file BODY_FILE] [--append] [--create-if-missing]
            [--dry-run] [--format {text,json}] [--group-id GROUP_ID] [--actor ACTOR]
```

Block-level partial update: replace (or append to) ONE section of a
concept body, keeping the rest of the document — and all frontmatter —
untouched. Exactly one of `--body`/`--body-file` is required. Thin
wrapper over the `update_section` update op.

| Flag | Effect |
|---|---|
| `--heading HEADING` | Section to target. Level-pinned (`"## Banking"`) or bare (`"Banking"`, matches any level); case-insensitive. |
| `--append` | Append to the end of the section instead of replacing it. |
| `--create-if-missing` | Create the section at the end of the body when the heading is absent (success reason `section_created`). |

The section span includes its subsections (up to the next heading of
the same or higher level). A leading copy of the target heading in the
fragment is stripped, so a fragment file may repeat the heading.
Headings inside fenced code blocks are ignored.

| Outcome | Reason | Exit |
|---|---|---|
| Heading absent (without `--create-if-missing`). | `section_not_found` | `1` |
| Duplicate matches — pin the level to disambiguate. | `section_ambiguous:N` | `1` |
| Content already as requested (idempotent no-op). | `section_unchanged` | `0` |

# replace-text

```bash
scripts/okf-loom replace-text --bundle BUNDLE --id ID
            [--old OLD] [--old-file OLD_FILE] [--new NEW] [--new-file NEW_FILE]
            [--all] [--dry-run] [--format {text,json}]
            [--group-id GROUP_ID] [--actor ACTOR]
```

Exact textual patch on a concept body: swap `--old` for `--new`.
Exactly one of `--old`/`--old-file` and exactly one of
`--new`/`--new-file` is required. Thin wrapper over the `replace_text`
update op. On success the reason string is `replaced N occurrence(s)`.

| Flag | Effect |
|---|---|
| `--all` | Replace every occurrence (default: more than one match fails closed). |

| Outcome | Reason | Exit |
|---|---|---|
| Zero matches. | `text_not_found` | `1` |
| More than one match without `--all` — add surrounding context to `--old` to pin one occurrence. | `text_ambiguous:N` | `1` |
| `--old` equals `--new` (idempotent no-op). | `same_value` | `0` |

# link-add

```bash
scripts/okf-loom link-add --bundle BUNDLE --source SOURCE --target TARGET
            [--label LABEL] [--section SECTION] [--relation RELATION]
            [--evidence EVIDENCE] [--allow-forward-reference] [--dry-run]
            [--format {text,json}] [--group-id GROUP_ID] [--actor ACTOR]
```

Append a markdown link in `--section` (or end of body). Always emits
the upstream OKF 0.2 §6.1 absolute form (`/tables/customers.md`).

| Flag | Effect |
|---|---|
| `--relation TYPE` | Also append a typed relation to the source's `relations:` list (activates `okf.cap.typed_relations`). |
| `--evidence TEXT` | Becomes the relation `detail`. |
| `--allow-forward-reference` | Permit a target that does not yet exist (upstream §6.1 consumer tolerance). Default authoring behavior: fail (exit `1`) on a missing target. |

See [links.md](/reference/links.md).

# entity-add

```bash
scripts/okf-loom entity-add --bundle BUNDLE --id ID --label LABEL [--kind KIND]
            [--entity-id ENTITY_ID] [--alias ALIAS] [--dry-run]
            [--format {text,json}] [--group-id GROUP_ID] [--actor ACTOR]
```

Append an entity object to a concept's `entities:` frontmatter list
(activates `okf.cap.entities`). Idempotent on `(label, kind)`:
re-running will not duplicate. `--alias` is repeatable; aliases feed
entity/semantic search.

# repair

```bash
scripts/okf-loom repair <bundle> [--apply] [--indexes] [--mirror-relations] [--all]
            [--group-id GROUP_ID] [--actor ACTOR] [--format {text,json,md,dot}]
```

Mechanical fixes only (current spec §7). Dry-run by default; pass `--apply`
to write. `--all` is the default scope when no filter is given.
Relation mirroring runs **before** index regen so regenerated
indexes reflect the new links.

# Output formats

When supported, `--format` accepts `text` (default) and `json` (the
agent/harness integration path).
JSON output is strict: unsupported or non-finite runtime values fail
deterministically rather than being coerced with `str()`, and no partial
successful JSON is written.
Some reporting commands also accept `md` and `dot`; `search` does not.
Context Pack formats remain exactly `json` and `markdown`.
Commands without `--format` either emit their command-specific text/JSON
shape (`wait` prints one JSON work item; `watch` uses `--emit`) or write
files as their primary output.

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [frontmatter.md](/reference/frontmatter.md) — the frontmatter contract.
- [links.md](/reference/links.md) — link forms and graph edges.
- [capabilities.md](/reference/capabilities.md) — capability registry.
- [search_modes.md](/reference/search_modes.md) — search backends.
- [context_packs.md](/reference/context_packs.md) — deterministic Agent Context Pack contract.
- [http_routes.md](/reference/http_routes.md) — HTTP server routes.
- [comment_lifecycle.md](/reference/comment_lifecycle.md) — comment state model.
- [config_yaml.md](/reference/config_yaml.md) — `okf-loom.config.yaml` keys.
- `scripts/okf-loom <command> --help` — authoritative per-command help.
- [`/reference/spec.md`](/reference/spec.md) — current build-from-docs specification.
