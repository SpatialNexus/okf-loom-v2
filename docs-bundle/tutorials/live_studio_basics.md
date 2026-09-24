---
type: Tutorial
title: Use the live studio
description: Bring up scripts/okf-loom serve, navigate the viewer (rendered, source, split, graph, search), post a comment, and watch it land in directives.jsonl.
resource: /tutorials/live_studio_basics.md
tags: [viewer, studio, serve, comments]
timestamp: "2026-06-29T00:00:00Z"
---

# Use the live studio

This tutorial brings up the OKF live studio in your browser and walks you
through the main reading surfaces, then shows you how a comment you
post becomes a directive the agent can act on.

The studio is the same viewer in every reading mode (rendered, source,
split, graph, search) plus a live-update layer and a comment channel.
You do not need an agent running to follow this tutorial — the studio
works on its own.

This tutorial assumes you have a small bundle to point at.
If you do not, finish [Author your first bundle](/tutorials/first_bundle.md)
first, or use the sample at `okf-loom/samples/demo_bundle`.

# What you will have at the end

By the end of this tutorial you will have:

- Served a bundle with `scripts/okf-loom serve`.
- Navigated the root index, a concept page, the source and split views,
  the graph view, and search.
- Posted a comment from the studio.
- Found that comment on disk in `directives.jsonl`.

You will be able to serve the studio and direct work via comments.

# Step 1: Serve the bundle

Point `scripts/okf-loom serve` at your bundle and open it without auto-launching a
browser (so you can control the URL yourself):

```bash
scripts/okf-loom serve ~/my-first-bundle --no-open
```

The studio prints a URL like the one below and then runs in the
foreground of that terminal:

```text
serving bundle on http://127.0.0.1:8787/
```

By default the studio binds to `127.0.0.1` only — your loopback
interface — so only you can reach it.
Binding to the network requires an explicit `--public` flag and an
acknowledgement, which this tutorial does not need.

Leave that terminal running.
Open a second terminal for the rest of the steps, and open the URL in
your browser.

# Step 2: Open the root index

The studio's landing page is the bundle root index.

In your browser, visit:

```text
http://127.0.0.1:8787/
```

You should see a dashboard. It leads with the bundle title and purpose,
a compact intro with stats (concepts, types, links), and one
concept-card inventory grouped by type. **Order** defaults to
**Recommended**; use **Recently changed**, **Title**, or the **Type**
filter to change the browse view. The graph preview is secondary, behind
a **Preview graph** disclosure. Cards show type identity, available
descriptions and link counts (→ / ←), and up to three tags.
While the studio runs you also get a **Recently changed** rail (the last
few edited concepts with relative times) and live comment-count chips on
cards that have open threads.

The route behind this page is `/`, documented in
[HTTP server routes](/reference/http_routes.md).

# Step 3: Open a concept page

Click any concept — for example **Orders**.

The page opens with a type-tinted header band: the type chip and icon, a
reading-time estimate, the description as a lede, alias subtitles, entity
chips, and any typed relations. Below the authored body, a collapsed
**Document details** disclosure exposes structured facts, followed by
**Links to** and **Cited by** connections; long connection inventories
collapse behind a counted disclosure. Related concepts remain a separate
recommendations area.

One near-header **On this page** outline starts collapsed. The document
utilities sit beside it (context actions, plus **Source** and
**Comments** in the live studio). The sticky document view controls also
offer **Sections**, which shows the current section and opens a popover
driven by the same outline state. Hovering a heading reveals a ¶
deep-link anchor.

The route is the concept id with the `.md` stripped — for example:

```text
http://127.0.0.1:8787/tables/orders
```

Try editing the file on disk while the studio is running and saving it.
The page updates in place without a full reload — that is the live-update
layer at work.

# Step 4: Switch to source and split view

The concept page has a view-mode switcher with three options:

| Mode | What it shows |
|---|---|
| Rendered | The markdown rendered as HTML. |
| Source | The raw markdown file, exactly as it sits on disk. |
| Split | Rendered on one side, source on the other. |

Switch to **Source** to see the frontmatter and body verbatim — useful
when you want to copy the exact text of a link or a schema row.

Switch to **Split** to compare the rendered view against the source side
by side.
This is the mode most authors settle into during a session.

# Step 5: Open the graph view

The studio renders the whole bundle as a live graph.

Visit:

```text
http://127.0.0.1:8787/__graph
```

The default **Map** lens uses a force-directed fCoSE layout: nodes use
type icons, **detected themes** (community detection) supply colour, and
PageRank supplies size. Other lenses change the layout, colour, or
ranking to answer their own questions.

The tour never starts automatically. In **Graph settings**, choose
**Take a 45-second graph tour** when you want an introduction. The
Details pane is reading-first: selected concept information and authored
content precede the lens controls and Graph settings. Open **Explore
with lenses** to switch lens; each lens answers one question and
provides a ranked summary:

| Lens | The question it answers |
|---|---|
| **Map** | How does this knowledge fit together? |
| **Themes** | What are the main topic areas — and which barely touch? |
| **Flow** | What feeds into what? (dependency layers, left → right) |
| **Bridges** | Which concepts hold the areas together? |
| **Recent** | What is fresh, what is going stale? |
| **Explore neighborhood** | What surrounds the selected concept? |

The collapsed **Graph settings** disclosure holds spacing and layout
controls, relationship bundles, counted type and relationship filters
with the graph key, relationship labels, and the colour-by-theme toggle.

Ways to explore:

- **Hover** a node: its neighbourhood stays lit while the rest fades,
  and a preview card shows the description and link counts.
- **Click** a node to read its selected-concept details and authored
  body in the Details pane, before the lens controls. Diagrams, math,
  and code use the same renderers as wiki pages. On compact screens, the
  **Concepts** list is the starting pane and explicit selection opens
  **Details**.
- **Shift-click** a second node to trace the shortest path between the
  two; the chain lights up and the status line reads it out.
- **Double-click** (or press Enter) to open the concept's wiki page.
- **Keyboard on the canvas**: `f` fits the view, `/` focuses graph
  search, arrows move the selection, and Esc clears. In the **Concepts**
  and relationship lists, Arrow/Home/End move the active item, Space
  selects, Shift+Space traces a path, and Enter opens.
- **Filters**: in **Graph settings**, use the counted type and
  relationship filters beside the graph key. Type filters include
  **Only** and **Show all** controls; there is no separate bottom-left
  canvas legend.

Concepts with no links sit apart with a dashed "not yet linked" outline —
this view is still the fastest way to spot orphans and clusters that
ought to be connected. If a file changes on disk while you watch, the
canvas updates in place and a "Graph updated" chip offers a re-layout.

# Step 6: Search

The studio search page is an evidence workbench, not just a title filter.
Type a word that appears in your bundle — for example `customer` — and
press Enter.
Each passage result shows one compact highlighted excerpt and an evidence
summary. Open **Evidence and context** to inspect the complete exact source
passage, citation such as `[tables/orders.md#L24-L31]`, ranking method, score,
reasons, and bounded graph evidence before using the result.

Try each matching mode:

| Workbench label | Mode | Use it for |
|---|---|---|
| **Best overall** | `hybrid` | Combined exact-word and meaning-oriented ranking. |
| **Exact wording** | `lexical` | Literal terms and identifiers. |
| **Similar meaning** | `semantic` | Related wording using dependency-free SemanticLite, not embeddings. |
| **Tagged with** | `tag` | Concept tags. |
| **Mentions entity** | `entity` | Entity labels; the Entity field belongs only to this mode. |
| **Connected by relation** | `relation` | Relation type, source, or target; those fields belong only to this mode. |

Open **Filters** to narrow by Type or Tag, set the result limit, or use the
mode-specific fields above.
Unsupported modes, malformed values, duplicate URL parameters, and filters
that do not apply to the selected mode are shown as errors; the studio does
not silently fall back to lexical ranking.

Behind the scenes the route is:

```text
http://127.0.0.1:8787/__search?q=customer
```

Search runs dependency-free out of the box.
The route defaults to lexical mode and a 20-result limit; the workbench can
run all six modes with a limit from 1 to 100.
The mode algorithms and result contract are described in
[Search modes](/reference/search_modes.md).

## Prepare a Context Pack from the results

After a search returns results, open **Build context pack**.
Keep the default 6,000-token budget and one graph-neighbour hop for this
exercise, then select **Build context pack**. The studio prepares a deterministic
schema-v1 Context Pack from the current query, mode, applicable filters, result
limit, and graph neighbourhood. Review its source, scope, estimated size, and
included/omitted counts, then select **Copy context pack**.

You can also prepare narrower packs:

- Select **Copy passage** for the visible exact excerpt, or open **Evidence and
  context** and select **Copy concept context** to seed a pack with one concept.
- Open a concept, select exact rendered text, press `Ctrl+K` (or `⌘K`), and
  choose **Copy selection with citation**. The studio copies only when the
  selection maps to one exact canonical source location; it never guesses at
  an ambiguous attachment.
- Return to the graph, select a node, and choose **Copy concept context** to
  seed the pack from that concept and its relevant graph neighbourhood.

Every pack contains source excerpts rather than generated summaries, and its
citations identify canonical bundle-relative Markdown paths and physical
source lines. A pack is cited passages for reuse, not a summary, and copying
it sends nothing to the agent; to ask the agent for work, post a comment
(Step 7). If automatic clipboard access is unavailable, the studio shows
the exact text in a read-only field for manual copying.

For a direct live-server example, open deterministic Markdown in a new tab:

```text
http://127.0.0.1:8787/__context?q=customer&mode=hybrid&budget_tokens=6000&neighbors=1&limit=20&format=markdown
```

JavaScript-free live requests remain complete through this GET route.
A built static site's no-JS fallback is intentionally narrower: it supports
browsing and canonical per-concept Markdown context, but arbitrary search,
selection mapping, and neighbourhood preparation require JavaScript.
See [Context Packs](/reference/context_packs.md) for schema v1, citation,
budget, and surface details.

# Step 7: Post a comment

Use the **Comments** panel to direct work without leaving the browser; it
is one of several Studio panels. A comment asks for
work or discussion. It is saved on this bundle for readers and its agent;
Studio does not start an agent. An external agent picks it up through its
comment loop (`scripts/okf-loom wait`, then `comment-claim` and
`comment-resolve`). (The **Commands** control — shortcut `Ctrl+K`,
`⌘K` on a Mac — opens the command palette.)

Open the panel and type a short directive, for example:

```text
Add a link from orders to the checkout service.
```

Post it.
The comment appears in the panel as an open thread.

The composer says how precise a comment is before you send it:

- With text selected inside one passage (rendered, or Source view when it maps
  exactly) it is an **exact annotation**: pinned to that passage and, once
  sent, highlighted on the page, and re-checked when the page changes. The
  saved card shows **Attached**.
- With no selection it is a **page comment**: about the whole page, with no
  text highlighted. The saved card shows **Page comment**.
- A selection that cannot be pinned exactly (across blocks, in the graph
  preview, or in Source view when it does not map exactly) makes a page
  comment, and the selected text is quoted in the comment rather than attached
  to exact text.

Comments and annotation controls require JavaScript. With JavaScript disabled,
the live route intentionally renders authored concept content and canonical
context links only; it does not server-render comment threads.

If selected text later moves, changes, or becomes non-unique, the card shows
its derived anchor health (`stale`, `ambiguous`, or `invalid`) instead of moving
the highlight to the first match. Choose **Re-anchor**, select the exact
intended text in the rendered document, and save. A concurrent comment or
document change produces a refresh-and-reselect conflict; an uncertain save
offers safe retry with the same intent. A resolved card exposes **Jump**.

The required final verification capture set under
`docs/screenshots/annotations-v2/` is exactly `resolved.png`,
`stale-after-mutation.png`, `selecting.png`, `conflict.png`,
`save-failure.png`, `focus-visible.png`, `mobile-320.png`,
`forced-colors.png`, and `no-js.png`. With JavaScript disabled, only authored concept content
and one honest fallback notice render (`no-js.png`): comments, stored quote
text, anchor health, highlights, Jump, and Re-anchor are unavailable.

The adjacent schema-1 `manifest.json` must bind every image to a SHA-256 digest
and fully serialized consumed-file inventory entries (exact path, regular or
symlink type, Git mode, tracked state, content hash, and symlink-target
semantics). Tracked modes come fail-closed from `git ls-files --stage` (`100644`,
`100755`, or `120000`); untracked regular files map executable bits to `100755`
or `100644`, and untracked symlinks use `120000`. The manifest also binds source
revision, capture-script digest, browser, each
artifact's actual viewport and media mode, truthful proof classification, and
scenario assertions.
The complete proof directory is built and validated as a sibling, then published
with a recoverable atomic directory swap; any validation or rename failure
preserves the prior set. Inventory binds Git mode and exact detailed entries,
rejects unsupported filesystem objects, and fails closed when Git provenance
cannot be verified. Real body and text-quote sentinels are persisted and
confirmed through `/__comments` before no-JS capture. The public
workflow capture serves a temporary bundle and uses browser controls plus the
Studio's HTTP APIs to select and post a real comment, confirm canonical comment
state, mutate canonical Markdown, and observe stale recovery. Conflict and
save-failure screenshots use deliberate `409 concept_conflict` and `500`
response injection at the browser HTTP boundary and are classified only as
injected-envelope UI handling; separate server tests prove production
envelopes. The no-JS gate searches complete response HTML and DOM text for
unique persisted comment-body and quote sentinels. Capture generation and tests
use the same production validator, including the `python -O` gate.
Existing images or manifest bytes are not current-tree release proof until the
final tree is recaptured and the manifest validator passes in normal and
optimized Python. Only that final manifest gate promotes the exact nine-image
set above to release evidence.

You have posted a comment to direct the agent's content work.
The local Studio also offers **Import feedback note** (under **Feedback
notes** in Studio home, or from the command palette): readers of an
export built with `build --include-portable-feedback` or
`render --include-portable-feedback` can send you page-level JSON notes,
which you preview and then confirm at `/__feedback-import` to create an
ordinary page comment. This export option is off by default, supports
static and single-file output only, and does not authenticate claimed
names, times, or quotes. See
[Import a portable feedback note](/how-to/import_portable_feedback.md).

# Step 8: Watch the comment land in directives.jsonl

Every comment you post is appended to a JSON Lines file inside the
bundle's session directory.

From your second terminal, list the open directives:

```bash
scripts/okf-loom comment-list ~/my-first-bundle --state open
```

You should see the comment you just posted, with an id, a concept anchor,
a state of `open`, and the body you typed.

The same record lives on disk:

```bash
cat ~/my-first-bundle/.okf-loom/session/directives.jsonl
```

Each line is one comment object.
This file is the contract between the studio (where you write comments)
and the agent loop (which consumes them).

The comment states and the rules for moving between them are documented
in [Comment lifecycle and archive rules](/reference/comment_lifecycle.md).

# Step 9: Stop the studio

When you are done, return to the first terminal and stop the server with
`Ctrl-C`.

The session directory (`.okf-loom/session/`) stays on disk, so the comments
you posted are preserved.
The next `scripts/okf-loom serve` picks them back up.

**Optional proposal inspection:** start a local Studio with
`--enable-proposal-review` to expose the experimental **Proposals** panel
when the capability is active. It lists proposal revisions and shows one
revision's details with a side-by-side or unified diff; it has no decide,
withdraw, apply, or undo controls. Local `--no-edit` pages show a CLI
pointer instead, and public or exported pages have no panel. See
[Proposal Diff Review](/reference/proposal_diff_review.md).

# Where to go next

You can now serve the studio and direct work via comments.

The comments you posted are consumed by the agent loop — the canonical
shape is `scripts/okf-loom wait` → claim → write → resolve:

* [Direct the agent loop](/tutorials/author_with_agent.md) — drive the comment-to-resolve loop end to end.

When threads start to pile up, you will want to triage them:

* [Archive and resolve comment threads](/how-to/archive_threads.md) — the archive and resolution rules.

The machinery behind every route above is documented in:

* [HTTP server routes](/reference/http_routes.md) — every URL the studio serves.
* [CLI command reference](/reference/cli.md) — `scripts/okf-loom serve`, `scripts/okf-loom comment-list`, and friends.
* [Context Packs](/reference/context_packs.md) — deterministic, citation-ready context for agents.

For the design rationale behind the studio's comment-driven model, see
[Why the live studio exists](/explanation/live_studio_design.md).

Return to [Tutorials](/tutorials/index.md).
