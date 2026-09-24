---
type: How-to
title: Embed the viewer in an agent harness
description: Embed okf-loom inside an agent harness (opencode, Claude Code, Codex, LangGraph, or custom) using the CLI shell-out shape, the Python library shape, or a long-running in-process server.
resource: /how-to/embed_in_harness.md
tags: [embedding, harness, integration, library, server]
timestamp: "2026-06-29T00:00:00Z"
---

# Embed the viewer in an agent harness

This guide shows the three integration shapes okf-loom supports and
when to reach for each.

You already have a bundle and a harness that wants to read or enrich it.
If you do not have a bundle yet, [Author your first bundle](/tutorials/first_bundle.md)
gets you started.

# Step 1: Pick an integration shape

| Shape | When to use |
|---|---|
| CLI shell-out | Your harness already runs shell commands and parses JSON. Simplest; no Python import. |
| Python library | Your harness is Python and wants in-process objects. |
| Long-running server | Your harness wants a live URL (browser, scraper, or a long-running studio). |

All three use the same dependency-free retrieval and Context Pack contract;
pick by what your harness already speaks.

# Step 2: Embed as a CLI shell-out

The simplest shape: shell out to the checkout-local CLI and parse the JSON.

```bash
scripts/okf-loom validate path/to/bundle --format json
scripts/okf-loom discover path/to/bundle --out /tmp/plan.json
scripts/okf-loom update  path/to/bundle --plan /tmp/plan.json

# Search results retain the established result fields and add exact evidence.
scripts/okf-loom search path/to/bundle "customer order" \
  --mode hybrid --type Table --tag facts --limit 20 --format json

# Prepare schema-v1 agent context as JSON or deterministic Markdown.
scripts/okf-loom context path/to/bundle "customer order" \
  --mode hybrid --type Table --tag facts \
  --budget-tokens 6000 --neighbors 1 --limit 20 --format json
scripts/okf-loom context path/to/bundle --concept tables/orders \
  --budget-tokens 2000 --neighbors 1 --format markdown
```

Use command-specific JSON/report flags where the command exposes them; a thin
wrapper that captures stdout and `json.loads` it is enough for those report
commands.

This is the shape most agent harnesses start with — it needs no Python
dependency in the harness itself.
Treat `schema_version: 1` as an explicit input contract, retain each item's
`citation`, and check `budget`, `included`, `omitted`, and `truncated` before
placing the pack in a model context window.
The excerpts are exact bundle source; they are not generated summaries.

For the harness-specific recipes (opencode, Claude Code, Codex,
LangGraph, custom), see
[/reference/embedding_guide.md](/reference/embedding_guide.md).

# Step 3: Embed as a Python library

When the harness is Python, import okf-loom directly and skip the
process boundary.

```python
from okf_loom import Bundle
from okf_loom.validate import validate_bundle
from okf_loom.discover import discover_suggestions
from okf_loom.update import load_plan, apply_plan
from okf_loom.retrieval import ContextRequest, RetrievalService, SearchRequest

bundle = Bundle.load("/path/to/bundle")

# Validate
report = validate_bundle(bundle)
print("Conformant:", report.ok, "Warnings:", len(report.warnings))

# Discover gaps
suggestions = discover_suggestions(bundle, rules=["unlinked_mentions"])

# Apply a plan (idempotent)
plan = load_plan("/tmp/plan.json")
apply_plan(bundle, plan)
bundle = bundle.reload()

# Read-only search and deterministic context over this Bundle snapshot.
retrieval = RetrievalService(bundle)
results = retrieval.search(SearchRequest(
    query="customer order", mode="hybrid", type_filter="Table", limit=20,
))
pack = retrieval.context(ContextRequest(
    query="customer order", mode="hybrid", type_filter="Table",
    budget_tokens=6000, neighbors=1, limit=20,
))
assert pack["schema_version"] == 1
for item in pack["items"]:
    print(item["citation"], item["excerpt"])
```

The library surface is the same pipeline the CLI uses, so behaviour is
identical between the two shapes.

`RetrievalService` is read-only and pins its answers to one `Bundle` snapshot.
After an on-disk edit, prefer `bundle = bundle.reload()` (or load a new
`Bundle`) and construct a new service.
If your integration mutates files or bundle state through another in-process
path, call `bundle.invalidate()` before the next read so graph, content,
backend, passage, and revision caches are evicted together.

# Step 4: Embed as a long-running server

For a live URL — a browser pointed at the viewer, a scraper, or a
long-running studio — embed the HTTP server in a daemon thread inside
the harness.

```python
import threading
from http.server import ThreadingHTTPServer
from okf_loom import Bundle
from okf_loom.server import OKFWikiHandler

bundle = Bundle.load("/path/to/bundle")
httpd = ThreadingHTTPServer(("127.0.0.1", 8787), OKFWikiHandler)
httpd.bundle = bundle
threading.Thread(target=httpd.serve_forever, daemon=True).start()
```

The harness can now point a browser at `http://127.0.0.1:8787/`, or
fetch the JSON data routes directly.

```bash
curl --get http://127.0.0.1:8787/__search \
  --data-urlencode 'q=customer order' \
  --data 'mode=hybrid' --data 'type=Table' \
  --data 'limit=20' --data 'format=json'

curl --get http://127.0.0.1:8787/__context \
  --data-urlencode 'q=customer order' \
  --data 'mode=hybrid' --data 'budget_tokens=6000' \
  --data 'neighbors=1' --data 'limit=20' --data 'format=json'

curl --get http://127.0.0.1:8787/__context \
  --data 'concept=tables/orders' --data 'neighbors=1' \
  --data 'format=markdown'
```

`/__search` supports all six modes and returns a bare result array on JSON
success for compatibility.
`/__context` returns schema-v1 JSON or deterministic Markdown.
Both routes reject malformed encoding, duplicate or unknown scalar
parameters, unsupported modes/formats, and mode-inapplicable filters instead
of broadening or falling back.
Keep canonical `/` separators literal in concept identifiers.

These GET routes are read-only but unauthenticated on a reachable server.
Bind to loopback unless you deliberately choose a public deployment, and do
not treat retrieval output as an authorization filter for sensitive bundle
content.

Swap the bundle at runtime by reassigning `httpd.bundle`; the next
request picks it up.

# Step 5: Point a browser at the live studio

When you want the full collaborative studio — comments, presence, live
SSE updates — run `serve` instead of the bare server.

```bash
scripts/okf-loom serve path/to/bundle --no-open
```

The harness hands the user `http://127.0.0.1:8787/` and then drives the
comment loop via the CLI mutators.

See [Direct the agent loop](/tutorials/author_with_agent.md) for the
canonical wait → claim → write → resolve flow, and
[/reference/embedding_guide.md](/reference/embedding_guide.md) for
harness-specific wiring.

# Step 6: Read the CSRF token when you must POST directly

The CLI mutators attach the per-session CSRF token for you.
If you POST to `/__apply`, `/__undo`, `/__presence`, or `/__comment`
over HTTP yourself, read the token the studio wrote on boot.

```bash
scripts/okf-loom serve path/to/bundle --no-open &
TOKEN=$(scripts/okf-loom token path/to/bundle)

curl -s -X POST http://127.0.0.1:8787/__presence \
  -H "Content-Type: application/json" \
  -H "X-OKF-Token: $TOKEN" \
  -d '{"state": "watching", "actor": "agent"}'
```

The token is per-session, mode `0600`, lives only in
`<bundle>/.okf-loom/session/.token`, and is readable only by the user that
launched `serve`.

`token` exits non-zero with a clear message if no session exists.

# End state

You now have okf-loom wired into your harness in the shape that fits
it best, and you know where to deepen the integration when you need the
live studio or the library API.

# See also

* [Embedding guide](/reference/embedding_guide.md) — harness-specific recipes for opencode, Claude Code, Codex, LangGraph.
* [Direct the agent loop](/tutorials/author_with_agent.md) — the comment-driven studio loop.
* [Build a static site](/how-to/build_static_site.md) — ship a bundle as a hostable site when you do not need a live server.
* [CLI command reference](/reference/cli.md) — every command the shell-out shape can call.
* [HTTP server routes](/reference/http_routes.md) — every route the server shape exposes.
* [Context Packs](/reference/context_packs.md) — schema v1, exact citations, budgets, and failure handling.
* [okf-loom architecture](/explanation/architecture.md) — why okf-loom is harness-agnostic by design.

Return to [How-to guides](/how-to/index.md).
