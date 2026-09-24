---
type: Reference
title: Embedding Guide
description: How to wire okf-loom into any agent / meta-harness — CLI shell-out, Python library, and long-running server shapes, with harness-specific recipes for opencode, Claude Code, Codex, LangGraph, MCP, and custom harnesses.
resource: /reference/embedding_guide.md
tags: [embedding, harness, integration, opencode, claude-code, codex, langgraph]
timestamp: "2026-06-29T00:00:00Z"
---

# Embedding okf-loom in an Agent Harness

This guide shows how to wire okf-loom into any agent / meta-harness.
okf-loom is **harness-agnostic by design**: no agent-framework code
lives in this repository. The intended audience is **agents in opencode /
Claude Code / Codex that point at the checkout-local `scripts/okf-loom` CLI** — so Shape 1 (CLI
subprocess) is the primary path. Shape 2 (Python library import) and
Shape 3 (embedded HTTP server) are advanced seams for harness authors who
need them. Shape 4 covers the **live studio** agent loop (built on
shapes 1–3).

Pick the shape that matches how your harness already works:

| Shape | When to use | Pros | Cons |
|---|---|---|---|
| **1. CLI subprocess** *(primary)* | Your harness already shells out to CLIs (most do — including opencode, Claude Code, Codex) | Zero coupling; JSON I/O; language-agnostic | Process-spawn latency per call |
| **2. Python library** *(advanced)* | Your harness is Python and you want in-process caching / the full API without subprocess overhead | No subprocess; in-process caching; full API | Couples to Python |
| **3. Embedded server** | You want a long-running wiki the harness can point a browser at | Interactive; hot-reload; same process as the agent | One server per bundle |
| **4. Live studio & agent loop** | You want the collaborative studio: user directs via comments, agent authors the bundle live | Real-time collaboration; headless `scripts/okf-loom watch` feed; in-process event bus | One server per bundle; agent must watch + write via mutators |

> **Read [`SKILL.md`](../../SKILL.md) first** if you are an agent — it is
> the loadable skill entrypoint. Focused skill resources live under
> [`resources/`](../../resources/).

---

## Shape 1 — CLI subprocess

The simplest integration. Your agent constructs a command, runs it, and
parses JSON on stdout. Most harnesses already do this for `git`, `rg`,
`npm`, etc.

```bash
OKF_REPO=/path/to/okf-loom

# Validate (returns 0 ok / 1 conformance fail / 2 strict fail)
"$OKF_REPO/scripts/okf-loom" validate /path/to/bundle --format json

# Inspect
"$OKF_REPO/scripts/okf-loom" info /path/to/bundle --format json

# Find gaps (writes a JSON report)
"$OKF_REPO/scripts/okf-loom" discover /path/to/bundle --out /tmp/plan.json

# Apply an update plan (idempotent)
"$OKF_REPO/scripts/okf-loom" update /path/to/bundle --plan /tmp/plan.json --format json

# Retrieve exact passage evidence.
"$OKF_REPO/scripts/okf-loom" search /path/to/bundle "customer order" \
  --mode hybrid --type Table --limit 20 --format json

# Prepare bounded schema-v1 agent context as JSON or deterministic Markdown.
"$OKF_REPO/scripts/okf-loom" context /path/to/bundle "customer order" \
  --mode hybrid --budget-tokens 6000 --neighbors 1 --limit 20 --format json
"$OKF_REPO/scripts/okf-loom" context /path/to/bundle --concept tables/orders \
  --budget-tokens 2000 --neighbors 1 --format markdown

# Stand up the viewer (blocks; the harness can run it in a side-thread or background process)
"$OKF_REPO/scripts/okf-loom" serve /path/to/bundle --port 8787 --no-open
```

Report commands document their JSON mode in `$OKF_REPO/scripts/okf-loom <cmd> --help`;
server/watch/render/build commands expose command-specific output flags instead.
This shape is **language-agnostic** — a Node, Go, or Rust agent can use
okf-loom identically.

### Discovery → Update loop (the typical agent task)

```bash
# 1. Find what's missing
scripts/okf-loom discover /path/to/bundle --format json > /tmp/discovery.json
# (the agent reads discovery.json, decides which suggestions to accept,
#  and writes /tmp/plan.json with the matching UpdateOp entries — see
#  docs-bundle/reference/cli.md and resources/advanced-operations.md for the op-kind reference)

# 2. Preview
scripts/okf-loom update /path/to/bundle --plan /tmp/plan.json --dry-run

# 3. Apply
scripts/okf-loom update /path/to/bundle --plan /tmp/plan.json

# 4. Re-validate
scripts/okf-loom validate /path/to/bundle --format json
```

---

## Shape 2 — Python library *(advanced)*

> This is an **advanced seam** for harness authors building a dedicated
> Python integration that wants in-process caching or the full library API
> without subprocess overhead. The primary embedding path for agents in
> opencode / Claude Code / Codex is **Shape 1 (CLI subprocess)** above —
> those agents use the `okf` CLI, they do not `import okf_loom`.

For Python harnesses (LangGraph tools, custom agents) that need it.

```python
from okf_loom import Bundle
from okf_loom.validate import validate_bundle
from okf_loom.discover import discover_suggestions
from okf_loom.update import load_plan, apply_plan
from okf_loom.retrieval import ContextRequest, RetrievalService, SearchRequest

bundle = Bundle.load("/path/to/bundle")

# Validate
report = validate_bundle(bundle)
if not report.ok:
    for f in report.errors:
        print(f.path, f.code, f.message)

# Discover gaps
discovery = discover_suggestions(bundle, rules=["unlinked_mentions", "orphan_concepts"])
for s in discovery.suggestions:
    print(s.rule, s.concept_id, s.action, s.message)

# (your code converts accepted suggestions to UpdateOp entries and
#  writes a plan file — see docs-bundle/reference/cli.md and resources/advanced-operations.md)

# Apply a plan
plan = load_plan("/tmp/plan.json")
summary = apply_plan(bundle, plan)
print(summary["applied"], "applied;", summary["skipped"], "skipped")
bundle = bundle.reload()

# Read-only search and context over one Bundle snapshot.
retrieval = RetrievalService(bundle)
for r in retrieval.search(SearchRequest(query="customer order", mode="hybrid", limit=5)):
    print(r.score, r.concept_id, r.title)

pack = retrieval.context(ContextRequest(
    query="customer order", mode="hybrid",
    budget_tokens=6000, neighbors=1, limit=20,
))
assert pack["schema_version"] == 1
for item in pack["items"]:
    print(item["citation"], item["excerpt"])
```

**Cache invalidation.** Graph, content, ranking backend, passage, and revision
indexes are derived and memoized on the `Bundle` snapshot.
After mutating the bundle via `apply_plan`, use `bundle = bundle.reload()` (or
load a new `Bundle`) and create a new `RetrievalService`.
An integration that mutates through another in-process path can call
`bundle.invalidate()` before the next read.
`okf_loom.search.clear_search_cache()` is a deprecated no-op kept for
import compatibility.

---

## Shape 3 — Embedded HTTP server

Run the live viewer inside the agent process so a browser (the user, a
scraper, an MCP browser tool) can navigate the bundle while the agent
works on it.

```python
import threading
from http.server import ThreadingHTTPServer
from okf_loom import Bundle
from okf_loom.server import OKFWikiHandler

bundle = Bundle.load("/path/to/bundle")
httpd = ThreadingHTTPServer(("127.0.0.1", 8787), OKFWikiHandler)
httpd.bundle = bundle                       # the handler reads this attribute
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# Later, after the agent has mutated the bundle on disk:
httpd.bundle = Bundle.load("/path/to/bundle")  # hot-swap; next request sees it
```

Routes are documented in [HTTP routes](/reference/http_routes.md) and
[advanced operations](../../resources/advanced-operations.md). The most useful for
agents:

- `/<concept_id>` — rendered concept page.
- `/__raw/<concept_id>` — raw Markdown when an unbounded whole-file read is
  specifically required.
- `/__data/graph.json` — the link graph as JSON (for the agent to reason
  about structure).
- `/__data/content.json` — the full content index as JSON.
- `/__search?q=customer&mode=hybrid&limit=20&format=json` — all-six-mode
  search with additive exact passage evidence.
- `/__context?q=customer&mode=hybrid&budget_tokens=6000&neighbors=1&format=json`
  — deterministic schema-v1 context for a query.
- `/__context?concept=tables/orders&neighbors=1&format=markdown` — concept
  and relevant graph-neighbour context as citation-ready Markdown.

Prefer `/__context` over dumping `/__raw` or `/__data/content.json` into a
model window.
Validate `schema_version == 1`, retain every `[path#Lx-Ly]` citation, and
honour `budget.estimated_tokens`, `included`, `omitted`, and `truncated`.
The item excerpts are exact source text; okf-loom does not invent summaries.

Search and context GETs are read-only but unauthenticated wherever the server
is reachable.
They reject malformed encoding, duplicate/unknown parameters, unsupported
modes, and mode-inapplicable filters.
Consumers must reject a response whose `schema_version` they do not support;
integrations must surface either class of error rather than retrying with a
broader lexical query or uncited content.

---

## Shape 4 — Live studio & the agent loop

`scripts/okf-loom serve` runs a **live collaborative studio**: the user
**directs** (reads + comments) and an **external agent authors** the OKF
files in real time. The server stays LLM-free — it emits events and runs
mechanical ops; the agent (in opencode / Claude Code / Codex / any harness)
does the thinking and writes back through the **existing mutators**. This
shape is how an agent harness wires into that loop.
Serve the studio proactively when the user wants to see, review, or comment on
the docs, then follow the canonical bring-up posture in
[`/reference/spec.md`](/reference/spec.md) §10.
Use the foreground `wait` → `comment-claim` → mutators → `comment-resolve`
loop from §12; `wait` is block-once and must not be backgrounded.

### 4a — Headless feed with `scripts/okf-loom watch` (continuous tail)

`scripts/okf-loom watch` reuses the bundle watcher and prints one event per line (the §7.2
schema) for a long-running consumer to pipe to the agent. No HTTP server
needed.

```bash
# Tail every change to the bundle as JSON Lines
scripts/okf-loom watch /path/to/bundle --emit jsonl

# Reconnect-safe: replay missed events from an id, then tail live
scripts/okf-loom watch /path/to/bundle --emit jsonl --since 01JABED5K7

# Let okf-loom run mechanical repairs on change (debounced), debounced 600ms
scripts/okf-loom watch /path/to/bundle --auto-repair --debounce-ms 600
```

`serve` and `watch` both append the durable, replayable
`<bundle>/.okf-loom/session/events.jsonl` — the audit trail and how a headless
agent "sees" the user's saves.

### 4a′ — Foreground block-once with `scripts/okf-loom wait` (the primary agent loop)

When **the agent itself is the consumer** (the common case in opencode /
Claude Code / Codex), prefer `scripts/okf-loom wait` over `scripts/okf-loom watch`. It **blocks until
there is work** (a new open comment and/or a change event), prints that one
item as JSON, and exits — so the agent wakes, acts, and re-waits in a clean
foreground loop:

```bash
# Blocks until a new OPEN user comment, prints it, exits 0. AWAIT this —
# do NOT background it (you are the processor; a backgrounded wait has no
# consumer, so comments would sit open forever).
scripts/okf-loom wait /path/to/bundle --for comment

# Wait for comments OR proactive-enrichment changes (whichever first):
scripts/okf-loom wait /path/to/bundle --for comment change --timeout 600
```

Library equivalent: `okf_loom.studio.wait_for_work(bundle_root, kinds=...,
since=..., timeout=..., interval=...)`. The loop is: `scripts/okf-loom wait` → read JSON →
reason → edit via mutators (`scripts/okf-loom link-add` / `update` / `entity-add` /
`repair`) → resolve the comment → `scripts/okf-loom wait` again.

### 4b — The comment → claim → write → resolve flow

The user's only studio writes are **comments / asks** (`POST /__comment`),
which land in `<bundle>/.okf-loom/session/directives.jsonl`. The agent's loop
(run `scripts/okf-loom wait` in your foreground — you are the processor):

```bash
# 0. Studio must be up so the session dir + .token exist.
scripts/okf-loom serve /path/to/bundle --no-open &

# 1. Block-once for the next OPEN comment.
scripts/okf-loom wait /path/to/bundle --for comment      # → {id, concept, anchor, body, …}

# 2. Claim it + announce presence.
scripts/okf-loom comment-claim /path/to/bundle 01JABED9K0
scripts/okf-loom presence     /path/to/bundle --state editing --focus tables/orders

# 3. Author via the mutators. With a live session the CLI routes through
#    Studio.save_concept — each op is attributed (actor=agent, origin=mutator),
#    undoable, and emits 'changed' + 'graph'. --group-id groups the pass for
#    one-click group undo (§12.5).
scripts/okf-loom plan     /path/to/bundle --scope tables/orders --neighbors --out /tmp/p.json
scripts/okf-loom link-add --bundle /path/to/bundle --source tables/orders \
             --target tables/customers --relation depends_on --group-id PASS1
scripts/okf-loom entity-add --bundle /path/to/bundle --id tables/orders \
               --label "Order" --kind Table --group-id PASS1
scripts/okf-loom update   /path/to/bundle --plan /tmp/p.json --group-id PASS1

# 4. Resolve the comment, linking the activity ids the writes produced.
scripts/okf-loom comment-resolve /path/to/bundle 01JABED9K0 \
    --activity 01JABED5K7,01JABED5K8 --reply "Done - added depends_on + Order entity"

# 5. Loop: scripts/okf-loom wait again.
```

Each write flows file → `Studio.save_concept` → watcher → live update
(SSE) + a change-list entry + an undo snapshot — the user sees it appear
in place, with a highlight, no full refresh.

The per-session CSRF token (`X-OKF-Token`) and Origin/Host allow-list guard
every mutating endpoint (current spec §14). The CLI mutators and the
`comment-*` / `presence` / `token` commands attach the token
for you. If you must POST to `/__apply` / `/__undo` / `/__presence` /
`/__comment` over HTTP directly, read the token via `scripts/okf-loom token
/path/to/bundle` (it reads `<bundle>/.okf-loom/session/.token`, mode 0600, set
on `scripts/okf-loom serve` boot) — never hand-copy it:

```bash
TOKEN=$(scripts/okf-loom token /path/to/bundle)
curl -s -X POST http://127.0.0.1:8787/__presence \
     -H 'Content-Type: application/json' \
     -H "X-OKF-Token: $TOKEN" \
     -d '{"actor":"agent","state":"editing","focus":"tables/orders"}'
```

For a fully headless integration (no `scripts/okf-loom serve` running), the CLI
mutators fall back to the non-studio atomic write path: same data-safety,
but no attribution / undo / live push. To get the live collaboration
surface, run `scripts/okf-loom serve` (or any process that creates `.okf-loom/session/`)
before invoking the mutators.

### 4c — Embedding the studio in-process (Python)

For a Python harness that wants the studio, the event bus, and the watcher
**in the same process** (no subprocess, no HTTP round-trip):

```python
import threading, queue
from okf_loom.studio import Studio
from okf_loom.watch import run_watch

# 1. Construct (or reuse) the studio handle for a bundle.
studio = Studio.for_bundle(
    "/path/to/bundle",
    session_rel=".okf-loom/session",   # default
    log_edits=True,               # append an upstream OKF 0.2 §9 log.md entry per write
    bundle_name="My Bundle",
    max_queue=64,
)
studio.ensure_session()

# 2. Subscribe to in-process events (returns a bounded queue.Queue you poll).
bus_q: queue.Queue = studio.bus.subscribe()
def _drain():
    while True:
        event = bus_q.get()        # blocks until an event arrives
        print("studio event:", event)   # {type, rev, ids, actor, ...} (§7.2)
threading.Thread(target=_drain, daemon=True).start()

# 3. Publish from your own code if you drive the bundle programmatically:
studio.bus.publish({"type": "presence", "actor": "agent", "state": "watching"})

# 4. Headless feed as a library call (same loop `scripts/okf-loom watch` runs):
#    run_watch("/path/to/bundle", emit="jsonl", since=None,
#              auto_repair=False, debounce_ms=600)  # -> exit code (blocks)
```

`run_server(..., studio_edit=True, studio_live=True)` (see Shape 3) attaches
`server.studio` — the `Studio` instance (or `None` if both are off) — so a
daemon-thread embedder can reach `httpd.studio.bus.subscribe()` for events
without launching `scripts/okf-loom watch` separately.

#### `EventBus.subscribe(callback)` — the in-process fan-out shape (§17)

`subscribe()` also takes an optional **callback** for the simplest in-process
embedding — no queue to poll, no thread to spawn:

```python
from okf_loom.studio import Studio

studio = Studio.for_bundle("/path/to/bundle")
studio.ensure_session()

# One subscription = one callback invoked per published event from any writer
# (the CLI mutators in this process, the watcher's disk-backfill, your own
# publish() calls). Returns an opaque sub handle you pass to unsubscribe().
sub = studio.bus.subscribe(callback=lambda ev: print("event:", ev))

# ... later, when you no longer care:
studio.bus.unsubscribe(sub)
```

The callback runs on the publisher's thread, so keep it cheap (hand off to a
worker queue if you do heavy work). The bus delivers events in publish order;
a slow callback does not block other subscribers (they each have their own
bounded queue, and a callback subscriber that lags gets a `resync` event).

#### `EventBus.subscribe(callback)` trust boundary (P3-12 / SEC2-007)

`subscribe(callback)` runs the **caller-supplied Python callable inside the
server process**. This is intentional — it is the §17 in-process embedding
seam — but it is also a **trust boundary equivalent to the bundle
`active-code` gate** (§15.4):

- **Bundle active-code** (the `{{ ... }}`/`{% ... %}` template blocks a
  bundle can embed) is sandboxed to the bundle directory and gated by the
  operator-consent flag. A bundle cannot reach `EventBus.subscribe` from
  inside the server — the bundle has no Python import surface, only
  rendered markdown.
- **`EventBus.subscribe(callback)`** is the harness-author surface. A
  callback you register has the **full privilege of the server process**:
  it can `import`, `open()`, `subprocess.run`, read
  `<bundle>/.okf-loom/session/.token`, and write to the bundle. The CSRF token,
  Origin/Host allow-list, and `--public` ack gate that protect the HTTP
  surface do **not** constrain a callback registered through this seam —
  they guard the *network* boundary, and the callback is *in-process*.

**Practical implications:**

1. **A malicious harness extension can execute arbitrary Python in the
   server process.** Treat any code that calls `studio.bus.subscribe(cb)`
   the same way you would treat code that calls `eval()` on the server:
   only register callbacks from code you trust. okf-loom does not
   sandbox the callback for you.
2. **When embedding in a larger harness** (opencode / Claude Code / Codex /
   LangGraph / a custom agent), do **not** forward untrusted network input
   directly into a callback. If you need to expose bus events to an
   untrusted consumer, use the queue form (`bus.subscribe()` with no
   callback) and serialize events over a boundary you control (a pipe, a
   file, an HTTP endpoint with its own auth).
3. **Sandbox if possible.** If you have a reason to register an untrusted
   callback (e.g. a user-supplied plugin), run it in a subprocess or a
   `multiprocessing`-isolated worker and proxy events to it. Do not rely
   on Python's "no real sandbox" `exec` restrictions.
4. **Exception behavior.** The worker thread that invokes your callback
   **swallows exceptions by design** — a buggy callback must never kill the
   event bus (§13.8 robustness). Swallowed exceptions are logged to
   **stderr** with the prefix `[okf-callback]` so they are not silent.
   Redirect the server process's stderr to your log collector to capture
   them:

   ```bash
   scripts/okf-loom serve /path/to/bundle 2>>/var/log/okf-callbacks.log
   ```

   If you want exceptions to surface more aggressively (e.g. fail a test
   suite), wrap your callback:

   ```python
   _seen = []
   def safe_cb(event):
       try:
           my_logic(event)
       except Exception as e:
           _seen.append(e)   # assert on _seen in your test

   sub = studio.bus.subscribe(callback=safe_cb)
   # ... after the test:
   assert not _seen, f"callback raised: {_seen}"
   ```

The single-source guarantee is: **the bundle cannot register a callback
(it has no Python surface in the server); only the harness that owns the
server process can.** If you trust the harness, you trust its callbacks.

#### The §17 library API (all names implemented)

The HTTP layer is a thin wrapper over these — one implementation, whoever
the caller is. Use them when you embed the studio in-process instead of
shelling out to the CLI:

| Method | HTTP equivalent | Purpose |
|---|---|---|
| `studio.save_concept(*, concept_id, raw, actor="agent", action="write_concept", origin="mutator", group_id=None, expected_rev=None, summary=None, detail=None, undoable=True, emit_graph=None, path=None, publish=True) -> WriteResult` | `POST /__apply` (per-op) | **The single internal atomic write path.** Validates before write, snapshots for undo, atomically writes, appends a row to `events.jsonl`, broadcasts `changed` (+ `graph` for graph-affecting ops). Raises / returns a conflict on `expected_rev` mismatch (§9.3/§9.4). |
| `studio.post_presence(*, actor="agent", state="idle", focus=None) -> dict` | `POST /__presence` | Set agent presence (current spec §12). State ∈ `idle`/`watching`/`thinking`/`editing`. Broadcasts a `presence` event. |
| `studio.post_comment(*, concept, body, anchor=None, actor="user", detail=None) -> dict` | `POST /__comment` | Append a user comment / ask to `directives.jsonl` (§9). Returns the new directive. |
| `studio.resolve_comment(comment_id, *, reply=None, activity_ids=None, actor="agent") -> dict \| None` | (CLI: `scripts/okf-loom comment-resolve`) | Mark a comment `resolved` with optional reply + activity links (drives group undo, §12.5). |
| `studio.record_activity(*, actor, action, ids, summary, origin="mutator", undoable=False, group_id=None, detail=None, emit_graph=None) -> dict` | (SSE `activity` event) | Append an `activity` event row to `events.jsonl` and broadcast it. Use for actions that don't write a concept file but should still show on the change list. |
| `studio.events_append(event, *, publish=True) -> dict` | (writer helper) | The low-level append every other method uses. Adds the `id`/`ts`/`seq`, takes the advisory file lock, writes one JSON line to `events.jsonl`, optionally broadcasts. Use this only if you need a fully custom event. |
| `studio.post_suggestion(*, concept, action, args, summary, group_id=None, actor="agent") -> dict` | (SSE `suggestions` event) | **Only under a user's opt-in `propose_only` constraint** (§12.3). Appends a `PlannedAction` envelope to `proposals.jsonl` instead of writing. Absent by default. |

Plus `studio.list_comments(...)`, `studio.get_comment(id)`,
`studio.update_comment(id, …)`, `studio.set_presence(...)`,
`studio.get_presence()`, `studio.read_events(since=None, limit=None,
actor=None, concept=None)`, `studio.snapshot_for_undo(...)`,
`studio.restore_snapshot(...)`, `studio.undo_group(group_id)`,
`studio.mark_logged(id)`, `studio.current_rev(cid)`, and the module-level
`wait_for_work(bundle_root, *, kinds=..., since=..., timeout=...,
interval=...)` (the same loop `scripts/okf-loom wait` runs).

#### Client-side extension: `window.okfLoomStudio.register(kind, config)`

The browser studio exposes an additive, CSP-safe JavaScript extension seam
(current spec §15) mirroring the server `ViewerPlugin` intent but running
fully client-side after the page boots. `panel` (add a slide-over panel +
studio-bar toggle; the built-in Comments / Changes / Agent activity panels
use it) and `viewMode` (add a button to the Rendered/Source/Split switch)
ship wired; `toolbar`, `graphDecorator`, and `suggestionRenderer` are
reserved (accepted + `console.warn`, not wired). Load a `register(...)`
extension from a `.okf-loom/viewer/static/*.js` override or eval it from your
harness after the studio boots. See
`scripts/okf_loom/viewer/OVERRIDES.md` §6 for the full API and worked
panel/view-mode examples. The built-in Comments, Changes and Agent activity
panels also show one sentence under the panel title saying what the panel
answers, and the panel is described by it while it shows. `register()` is
unchanged, so extension panels get no such line.

#### The bundle-on-disk is the bus

The studio is **not** the source of truth — the `.md` files are. The studio
is the **fan-out + attribution + undo** layer over them. Every write goes:

```
your code ──▶ studio.save_concept ──▶ atomic write of <bundle>/<id>.md
                                       │
                                       ▼
                            .okf-loom/session/events.jsonl  (one row per write)
                                       │
                            ┌──────────┴───────────┐
                            ▼                      ▼
                    EventBus.publish        _BundleWatcher detects
                            │                mtime change, diffs the
                            ▼                ContentIndex, and (if the
                    SSE /__events           write didn't log itself)
                    fan-out to every         appends an origin:"disk"
                    open tab + every         event — so the feed is
                    in-process               complete regardless of
                    subscriber                which path produced it.
```

For an embedded harness this means: writes via `save_concept` (or the CLI
mutators) → `events.jsonl` → SSE + `EventBus` fan-out, all from the same
files a `scripts/okf-loom watch` would tail. A second harness process embedding the
same bundle sees the same events through the file bus — no IPC beyond the
filesystem. This is what keeps the studio headless-friendly (§1.9 of the
current spec) and lets the agent run externally while the studio runs
in-process for the user's browser.

### 4d — What to expose for the agent loop

At minimum, expose for the studio loop:

1. `scripts/okf-loom serve <bundle> --no-open` — bring up the live studio (hand the user
   the URL).
2. `scripts/okf-loom watch <bundle> --emit jsonl [--auto-repair]` — the headless change
   feed that drives proactive enrichment (§11).
3. The mutators (`scripts/okf-loom link-add` / `entity-add` / `update` / `repair` /
   `write-concept`) — how the agent actually authors files in response to
   comments and the watch feed.
4. `POST /__presence` (or a write to `.okf-loom/session/presence.json`) — so the
   user can see what their teammate is doing.

---

## Harness-specific recipes

### opencode

[opencode](https://opencode.ai) discovers project-scoped agents and
skills under `.opencode/` and an `AGENTS.md` at the repo root. To wire
okf-loom into an opencode project:

1. **Clone or vendor this repository** into the agent's workspace.
   The repo skill layout is the artifact; do not depend on package installation.
2. **Expose the root skill** under `.opencode/skill/okf-loom/` (or keep the
   vendored checkout there). The skill directory must include `SKILL.md`,
   `resources/`, `scripts/`, and `docs-bundle/` together:

   ```
   .opencode/skill/okf-loom/
   ├── SKILL.md
   ├── resources/
   ├── scripts/
   └── docs-bundle/
   ```

   opencode's skill loader will surface it; the resolver matches on
   the `description` triggers (e.g. "okf", "validate okf",
   "knowledge bundle").

3. **Copy or include `AGENTS.md`** from this repository to your project root
   only if your harness starts from AGENTS files. It points back to `SKILL.md`.

4. **Scope a subagent** for OKF work (`.opencode/agent/okf-curator.md`):

   ```markdown
   ---
   description: Curates, validates, and updates OKF bundles. Use for
     "okf", "validate knowledge", "discover missing links", "update
     okf bundle", "stand up okf viewer".
   mode: subagent
   tools:
     bash: true
     read: true
     write: true
     edit: true
   ---
   You are the OKF curator. Always load `okf-loom/SKILL.md` first,
   then read the focused resource for the task (`resources/format-basics.md`,
   `resources/authoring.md`, `resources/advanced-operations.md`, or
   `resources/studio-agent-loop.md`). Use the checked-in helper:
   `/path/to/okf-loom/scripts/okf-loom`.
   Run `scripts/okf-loom validate` from inside the checkout, or `okf-loom/scripts/okf-loom validate` from a parent workspace, before claiming completion; run `discover` to find
   gaps; apply suggestions via `update --plan`. Never bulk-convert existing
   content without user confirmation.
   ```

5. **Permission scoping** (opencode `permission.bash` patterns):

   ```json
   {
     "permission": {
       "bash": {
         "scripts/okf-loom validate *": "allow",
          "scripts/okf-loom info *": "allow",
          "scripts/okf-loom discover *": "allow",
           "scripts/okf-loom graph *": "allow",
           "scripts/okf-loom search *": "allow",
           "scripts/okf-loom context *": "allow",
           "scripts/okf-loom capabilities *": "allow",
          "scripts/okf-loom render *": "allow",
          "scripts/okf-loom build *": "allow",
          "scripts/okf-loom index * --dry-run": "allow",
          "scripts/okf-loom index *": "ask",
          "scripts/okf-loom log * --dry-run": "allow",
          "scripts/okf-loom log *": "ask",
         "scripts/okf-loom update * --dry-run": "allow",
         "scripts/okf-loom update *": "ask",
         "scripts/okf-loom serve *": "ask"
       }
     }
   }
   ```

### Claude Code

Claude Code reads `CLAUDE.md` at the repo root and skills under
`.claude/skills/`. The shape mirrors opencode:

1. Copy or symlink root `SKILL.md` into the harness skill directory.
2. Copy the `resources/`, `scripts/`, and `docs-bundle/` folders alongside it.
3. Claude Code's skill/resource discovery will surface the skill when the user
   mentions OKF / "knowledge bundle" / "validate okf" etc.

### Codex (OpenAI)

Codex reads `AGENTS.md` at the repo root (the same file opencode uses).
Skill conventions vary; the simplest path:

1. `AGENTS.md` at repo root (already in this repository; copy verbatim).
2. Reference `SKILL.md` from `AGENTS.md` so Codex reads the root skill and
   resources when relevant. If auto-discovery is unavailable, an explicit
   `load okf-loom/SKILL.md when the user asks about OKF` line works well.

### LangGraph

Wrap the CLI in tool functions:

```python
from langchain_core.tools import tool
import subprocess, json, pathlib

def _okf(*args):
    # The repo ships no installed console script; shell out to the checkout path.
    r = subprocess.run(["okf-loom/scripts/okf-loom", *args], capture_output=True, text=True, check=True)
    return r.stdout

@tool
def okf_validate(bundle_path: str) -> dict:
    """Validate an OKF bundle. Returns conformance report JSON."""
    return json.loads(_okf("validate", bundle_path, "--format", "json"))

@tool
def okf_discover(bundle_path: str) -> dict:
    """Find missing links/indexes/descriptions/relations/orphans in an OKF bundle."""
    return json.loads(_okf("discover", bundle_path, "--format", "json"))

@tool
def okf_update(bundle_path: str, plan_path: str, dry_run: bool = False) -> dict:
    """Apply an OKF update plan (add link/tag/relation/section). Idempotent."""
    args = ["update", bundle_path, "--plan", plan_path, "--format", "json"]
    if dry_run:
        args.append("--dry-run")
    return json.loads(_okf(*args))

@tool
def okf_search(bundle_path: str, query: str, mode: str = "lexical", limit: int = 10) -> list:
    """Search an OKF bundle with one of the six supported modes."""
    return json.loads(_okf("search", bundle_path, query, "--mode", mode,
                             "--limit", str(limit), "--format", "json"))

@tool
def okf_context(bundle_path: str, query: str, budget_tokens: int = 6000) -> dict:
    """Return schema-v1 exact, cited context within the requested budget."""
    pack = json.loads(_okf("context", bundle_path, query, "--mode", "hybrid",
                           "--budget-tokens", str(budget_tokens), "--format", "json"))
    if pack.get("schema_version") != 1:
        raise ValueError("unsupported OKF Context Pack schema")
    return pack
```

Wire these into your graph's tools list. The agent will reach for them
when the user mentions knowledge / docs / catalogs.

### MCP server (model context protocol)

The CLI is shaped for an MCP wrapper. A minimal MCP server exposes
`okf_validate`, `okf_discover`, `okf_update`, `okf_search`, `okf_context`,
`okf_render` as tools. The `--format json` outputs are already
JSON-serialisable. (There is no in-repo MCP server today; wrap the CLI
as shown.)

### Custom agent (any language)

For a non-Python agent, the **CLI subprocess** shape works from any
language. Construct the argv, run, parse `--format json` stdout. Treat
okf-loom like `git` or `rg`: a standard Unix CLI.

---

## What to expose to your agent

At minimum, expose these six commands as agent-callable tools (the
"trigger list" the agent uses to decide when OKF is relevant):

1. `scripts/okf-loom validate` — for any "validate / check / lint" intent.
2. `scripts/okf-loom discover` — for any "what's missing / find gaps / improve"
   intent.
3. `scripts/okf-loom update` — for applying the agent's proposed fixes.
4. `scripts/okf-loom search` — for any "find / show me / where is" intent over the
    knowledge.
5. `scripts/okf-loom context` — for bounded, citation-ready context from a
   query, concept, or graph neighbourhood.
6. `scripts/okf-loom serve` — for any "show me / browse / open the wiki" intent.

The agent note (`AGENTS.md`) is the routing layer — it tells the agent
WHEN to reach for OKF (the trigger list) and points to the skills for
HOW.

## Testing your embedding

Whatever shape you pick, verify:

1. **Round-trip safety.** Apply a no-op update plan to a bundle; the
   bundle's content should be unchanged (modulo YAML whitespace).
2. **Idempotency.** Apply the same plan twice; the second run reports
   `applied:0, skipped:N`.
3. **Strict mode in CI.** `scripts/okf-loom validate --strict` should fail the build
   on warnings.
4. **Hot-swap.** In Shape 3, mutate the bundle on disk, swap
   `httpd.bundle`, hit `/` again; the new content should appear.
5. **Context contract.** Reject unsupported schema versions, preserve exact
   citations, and prove the harness never exceeds the pack's declared budget
   or silently replaces an error with uncited whole-file content.

The `samples/demo_bundle/` or `samples/showcase/` directories are good
test corpora.

## Reference

- Current okf-loom spec: [`spec.md`](/reference/spec.md)
- Upstream base OKF SPEC 0.2: https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md
- Agent note: [`../../AGENTS.md`](../../AGENTS.md)
- Skill resources: [`../../resources/`](../../resources/)
- Architecture: [`architecture.md`](/reference/architecture.md)
- Context Packs: [`context_packs.md`](/reference/context_packs.md)
- Research: [`research.md`](/explanation/research.md)
- Override examples: [`../../scripts/okf_loom/viewer/OVERRIDES.md`](../../scripts/okf_loom/viewer/OVERRIDES.md)
