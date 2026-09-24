---
type: Explanation
title: Why the live studio exists
description: The rationale behind the live studio — comment-driven directing, no review gate by default, all-on defaults, SSE plus polling fallback, the separate archive track, and the single-user trust model.
resource: /explanation/live_studio_design.md
tags: [studio, rationale, comments, security, archive]
timestamp: "2026-06-29T00:00:00Z"
---

# Why the live studio exists

This essay explains the reasoning behind the live collaborative studio.
It is not the tutorial for using the studio — that is
[Use the live studio](/tutorials/live_studio_basics.md) and
[Direct the agent loop](/tutorials/author_with_agent.md) — and it is
not the neutral reference for its routes and state model — that is
[HTTP server routes](/reference/http_routes.md) and
[Comment lifecycle and archive rules](/reference/comment_lifecycle.md).
It is the *why* behind those surfaces.

The binding design document is the
[`current okf-loom spec`](/reference/spec.md); this essay
summarises its live-studio posture (§10) and decisions at a readable
length and links out for depth.

# The posture the studio is built on

The studio rests on a single founding claim, stated as decision **D8**
and design posture §1.1 of the spec:

> **The agent is the author and the manager of the OKF files. Writing
> OKF files *is* the agent's job.** Its mechanical *and* semantic edits
> apply directly through the mutators; they appear live and land in the
> change list with one-click undo. The user's safety net is
> **transparency plus undo**, not an approval step.

Everything else in the studio follows from that. If the agent is the
author, then the user does not need an editor — they need a *directing*
surface. If the agent edits freely, then the safety net has to be
*see-and-undo*, not *review-before-write*. If the user is the sole
admin, then nothing should gate them, and the only protections that
remain are the ones that defend against things that are *not* the user.

# Why comment-driven directing

The studio's primary user interaction is a **comment** — a short
directive the user attaches to a concept, a section, a text selection,
or a frontmatter key. The user does not hand-edit markdown in the
browser; they point and comment, and the agent authors the files.

| The alternative considered | Why the studio chose comments instead |
|---|---|
| A direct in-browser markdown editor | Introduces a user↔agent merge problem (concurrent edits to the same file), requires CRDT/OT, and still needs a separate channel to *tell* the agent what to do. The agent is the sole writer of `.md` files, so there is no merge problem to solve day-to-day. |
| A chat sidebar that dispatches commands | Collapses "where is this about" into "what is this about". A comment anchored to a schema row carries both: it names the target *and* the intent. |
| A queue of typed actions (button per verb) | Forecloses the open-ended asks ("rewrite this section from the spec", "extract these columns into their own concepts") that are the actual daily work. Free-text comments with an anchor cover all of them. |

A comment is the steering wheel; the change list plus undo (§12.2 /
§12.5 of the spec) is the review surface. The user reads in Rendered /
Source / Split — all read-only — and directs; the agent claims a
comment, does its scoped work, writes via the ordinary mutators, and
resolves it, linking the activity entries it produced. The canonical
loop is documented in
[Direct the agent loop](/tutorials/author_with_agent.md).

> The user can still edit files directly on disk in their own editor —
> that is their machine, and the watcher picks it up. The studio web UI
> is a *directing* surface, not a direct editor.

# Why there is no review gate by default

This is the most often-questioned choice, so it is worth being precise.

A review gate is the idea that the agent proposes edits and a human
approves each one before it lands. The studio ships *without* one, and
the reasoning is that a review gate optimises for the wrong failure
mode. The daily failure mode of an agent authoring a knowledge bundle is
not "the agent wrote something malicious and we stopped it" — the agent
is the user's own, running locally, on the user's own files. The daily
failure mode is "the agent wrote the wrong *good* edit, and the user
wants to revert it in one click."

That argues for a fast, visible **undo**, not a slow, gating **approve**.
So the studio invests in three things instead of a gate:

1. **A live change list** — a read-only timeline of everything that
   happened, with actor, action, affected concepts, and a timestamp.
2. **Per-write undo snapshots** — prior bytes captured before every
   agent write under `.okf-loom/session/history/`.
3. **Group undo** — a single agent pass that writes several files is
   recorded as one group (a shared `group_id`), so the user reverts the
   whole pass with one click, not file-by-file.

Studio enforces configured forbidden-path and unique-member group caps on its
write funnel, but is not a general policy engine. Mandatory review or policy
covering arbitrary filesystem writes remains an external-harness concern.
`propose_only` enables an explicit legacy suggestion append, not interception
of direct edits. See [exact constraint coverage](/reference/config_yaml.md#constraints)
and the separate [experimental proposal extension](/reference/proposal_diff_review.md).

# Why everything is on by default

`scripts/okf-loom serve <bundle>` — with no flags — opens the full studio: live SSE
updates, source and split views, commenting, directing, agent presence,
the change list, and undo. There is no `--edit` to unlock, no per-save
consent, and no permission prompt.

The reasoning is that the studio's value is *collaboration that feels
alive*, and collaboration you have to assemble out of flags is
collaboration that does not happen. Every default that is off by
default is a feature the user has to discover, and the studio's P0 bar
(spec §1.7) is judged on how alive it feels out of the box, not on how
many features it has behind toggles.

The defaults that remain are the ones that cost the user *zero friction*
and defend against things that are not the user (see the trust model
below). Editing, saving, and watching are never behind a gate; only
the loopback boundary, the cross-origin guard, and the active-code gate
are — and the first two are invisible to a local user.

# Why Server-Sent Events, with a polling fallback

Live push is Server-Sent Events (SSE) over plain HTTP, served from the
stdlib `ThreadingHTTPServer`. The spec records this as decision **D0**:
SSE plus REST, not WebSocket.

| The alternative considered | Why SSE won |
|---|---|
| **WebSocket** | Richer bidirectional channel, but the studio's traffic is asymmetric (server pushes change/presence/comment events; client sends occasional POSTs). WebSocket would add a dependency surface and a protocol layer for a need SSE already meets with one worker thread per stream under stdlib. |
| **Pure polling** | Simpler, but a polling interval short enough to feel live burns cycles doing nothing, and an interval long enough to be cheap feels sluggish. SSE pushes the moment a change lands. |

The one real-world failure of raw SSE is that some intermediaries —
Cloudflare quick tunnels, corporate proxies, certain CDNs — buffer the
`text/event-stream` response, so `EventSource` connects but never
receives frames. The studio handles this with a **starvation
watchdog**: if no SSE frame arrives within a grace window (~5s), the
client arms a polling fallback that fetches `/__data/events?since=…`
on a short interval and dispatches through the *same* handler path as
SSE. If SSE recovers, the poller stops. The no-refresh contract
therefore survives behind a proxy that would silently starve SSE
alone (spec §7.3).

This matters because the no-refresh contract is a P0 requirement, not
polish: a full-page reload after every agent edit would discard the
user's scroll position, selection, view mode, and open comment threads.
The studio patches the smallest affected region and preserves all of
that state (spec §7.3).

# Why archive is a separate track

A comment carries two independent pieces of state:

| Track | Field | Values | Meaning |
|---|---|---|---|
| Lifecycle | `state` | `open` / `claimed` / `resolved` / `dismissed` | Where the work is. |
| Visibility | `archived` | `true` / `false` | Whether the panel soft-hides the thread. |

Archiving a resolved comment keeps it resolved; unarchiving restores
the prior visibility without flipping `state`. Compatibility records with
`state == "archived"` are normalised on read to `archived=true,
state="open"` so old sessions migrate automatically.

The archive track also carries three server-side rules that make it
behave predictably:

- **Archive is root-only.** Replies cannot be archived independently;
  the whole thread archives together.
- **Archive is gated on whole-thread-resolved.** The server returns
  `409` with the unresolved ids if any comment in the thread is not yet
  `resolved`, so the user cannot accidentally hide unfinished work.
- **Replying to an archived thread auto-unarchives the parent.** Adding
  to a thread reopens it for business; archive is a soft hide, not a
  closed state.

The full state model is in
[Comment lifecycle and archive rules](/reference/comment_lifecycle.md);
the step-by-step recipe is in
[Archive and resolve comment threads](/how-to/archive_threads.md).

# Why anchors are derived and explicit to repair

Annotations v2 stores quote intent with the comment but derives health against
the current canonical Markdown. A quote is therefore visibly `resolved`,
`stale`, `ambiguous`, or `invalid`; only a unique resolution becomes a mark or
Jump target. The resolver intersects independent evidence instead of choosing
the first matching text, so reordered or duplicated prose cannot silently move
a directive to the wrong place.

Legacy text anchors stay readable and are never rewritten on load. They resolve
only when their exact text is globally unique. When context changes, the user
chooses **Re-anchor**, selects the intended rendered text, and saves an explicit
schema-2 anchor. Row-token/content-revision checks turn intervening comment or
document changes into a conflict and reselection flow rather than lost work.
The WAL and idempotency key make a lost response safely replayable.

SSE remains notification-only: its numeric cursor orders and deduplicates
frames, while reconnect gaps and polling takeover cause a canonical comment and
document refetch. This separation prevents transport state from becoming
anchor truth.

This annotation layer is intentionally JavaScript-dependent. Without
JavaScript, the server-rendered page keeps authored concept content visible and
shows one honest fallback notice, but it does not fetch or display comments,
quote intent, or anchor health and offers no highlights, Jump, or Re-anchor.
That boundary avoids implying that a static server-rendered notice has performed
the revision-qualified resolver and DOM projection work owned by the Studio
client.

# The trust model: gate the port and the code, not the person

The studio is single-user local-admin. There is exactly one human, and
they own everything — the bundle *and* the agent that works for them.
The studio **never gates, prompts, or flag-guards the user**. What it
keeps are the protections that defend against things that are *not* the
user, and the spec is explicit (§15) that these must cost the user zero
friction.

| Protection | What it stops | What it does NOT stop |
|---|---|---|
| **Loopback by default** (`127.0.0.1`) | Other machines reaching the studio. Exposing the port is an explicit `--public` plus an acknowledgement. | The user or their agent. |
| **Per-session CSRF token** (`X-OKF-Token`, written `0600`) | A random web page POSTing to the localhost studio behind the user's back. | The studio's own page, which carries the token automatically. |
| **Origin AND Host allow-list** | Cross-site and DNS-rebinding attacks (checking both closes a hole that forging only `Origin` could exploit). | The user's own requests from an allowed host. |
| **Two-layer active-code gate** | A *downloaded bundle* trying to run code (JS / full-HTML overrides / viewer plugins) in the user's browser. | Editing, saving, and watching — those are explicitly not behind this gate. |

The summary the spec arrives at — and that this essay will end on — is:

> **We gate the port and the bundle's code, never the user or their
> agent.** The user's safety net for agent-authored edits is the live
> change list plus (group) undo, not an approval step.

Data-safety invariants (validate-before-write, path containment, atomic
writes, optimistic per-doc `rev`) are kept too, but they are framed as
*correctness* guards, not *permission* guards: they stop corruption and
lost work, not access. The single collision case the studio has to
handle — the user editing a file directly on disk at the same moment
the agent writes it — surfaces non-destructively as a `409` with a
diff, never as a silent clobber (spec §9.3 / §9.4).

# Why the server stays LLM-free

A final choice worth naming is that **the server never runs the agent.**
The server emits events, runs mechanical operations (index regen,
relation mirroring), and serves the studio; all semantic work is done
by the external agent (opencode / Claude Code / Codex / a custom
harness), which watches the change feed, reacts to comments, reasons,
and writes back through the ordinary mutators.

The reasons are the current non-goals: the checkout runtime stays
harness-agnostic (no opinion on which agent framework consumes it),
keeps the primary runtime dependency-light (no model bundled, no GPU assumed, no
network required), and keeps the collaboration working headlessly
(`scripts/okf-loom watch` tails the same change feed with no browser at all). The
agent loop — `scripts/okf-loom wait` blocks in the foreground, the agent claims,
writes via mutators, resolves — is the same shape whether the studio
is open or not. See [Direct the agent loop](/tutorials/author_with_agent.md).

# Why the graph is organised into question-named lenses

The graph view once offered presets (Overview, Hubs, Bridges, Relations,
Types) over a raw layout dropdown. It looked configurable but revealed
little: colour restated the type schema, Hubs ranked raw degree, and the
layout picker was a list of algorithm names. The current lens set replaced
it after a survey of graph tools (Obsidian and its graph plugins, Neo4j
Bloom, Gephi, Kumu, InfraNodus, Linkurious, yFiles, TheBrain, Roam and
Logseq) and of the graph-visualisation research literature. These findings
shaped it:

* **Colour carries computed structure, not the schema.** Gephi's standard
  workflow, InfraNodus and Bloom converge on the same composite: force
  layout, community colour and centrality size. Layout proximity is only
  legible when other channels confirm it (Venturini, Jacomy and Jensen
  2021). Colouring by type restates what readers already know, so type
  moved to the node **icon** and colour shows **detected communities**
  (deterministic Louvain modularity).
* **Importance is encoded explicitly.** Raw degree finds loud nodes, not
  important ones, and readers take *position* as importance unless a metric
  is encoded (McGrath, Blythe and Krackhardt 1997). Map sizes nodes by
  **PageRank**; Bridges colours and sizes them by **betweenness**.
* **Group colouring has a cost, so it can be turned off.** Group encodings
  help cluster tasks but cost about 25% accuracy on plain topology tasks
  (Saket et al. 2014). The **Colour by theme** toggle in Graph settings
  switches back to type colours.
* **Positions are deterministic.** Layouts that move on every load are a
  common complaint about note graphs, and stable positions matter when
  readers re-find nodes (Archambault and Purchase 2012–13). Initial
  positions are seeded from sorted ids, layouts do not randomise, and
  community detection is order-deterministic.
* **One non-force layout earns its place: layered flow.** Layered layouts
  beat radial ones for reading hierarchy, while force layouts can beat
  layered ones for path-finding (Pohl, Schmitt and Diehl 2009). Flow is
  therefore the lens for reading direction; shift-click path tracing works
  in every lens.
* **Time is a useful channel.** Recent encodes staleness as a static colour
  ramp, because static encodings beat animation for analysis (Archambault,
  Purchase and Pinaud 2011).
* **Answers sit beside the picture.** Every lens shows a clickable ranked
  summary with a one-line verdict, as Graph-Analysis, InfraNodus and Kumu
  pair their graphs with ranked answers.
* **Local exploration is the most-praised mode.** Explore neighborhood keeps
  the surrounding concepts at full opacity and dims the rest instead of
  hiding it (Obsidian's local graph, Kumu Focus, TheBrain; Moscovich et al.
  2009).
* **Modes are named by the question they answer**, never by the algorithm.
  Each lens shows its question in the lens bar and in its tooltip.

| Lens | Question it answers | Encoding |
|---|---|---|
| Map (default) | How does this knowledge fit together? | force layout; colour = community, size = PageRank, icon = type |
| Themes | What are the main topic areas, and which barely touch? | strong community grouping; ranked themes with a gap verdict |
| Flow | What feeds into what? | dagre layered layout (fallback: directed breadthfirst); typed edges with labels |
| Bridges | Which concepts hold the areas together? | colour and size = betweenness; top bridgers highlighted |
| Recent | What is fresh, and what is going stale? | recency colour ramp; undated concepts stay neutral |
| Explore neighborhood | What surrounds the selected concept? | current layout; neighbourhood at full opacity, the rest dimmed |

The same evidence retired the layout dropdown (each lens owns its layout),
the Types preset (type is always visible as the icon, and the type filter
works in every lens) and the degree-based Hubs preset (PageRank sizing
supersedes it). All analytics run in the browser, lazily, and are cached
until the graph changes. The [spec](/reference/spec.md) owns the exact lens
contract; [Use the live studio](/tutorials/live_studio_basics.md) shows the
lenses in use.

# See also

* [Use the live studio](/tutorials/live_studio_basics.md) — bring up
  `scripts/okf-loom serve` and navigate every reading surface.
* [Direct the agent loop](/tutorials/author_with_agent.md) — the
  `scripts/okf-loom wait` → claim → write → resolve loop end to end.
* [Archive and resolve comment threads](/how-to/archive_threads.md) —
  the archive track in practice.
* [HTTP server routes](/reference/http_routes.md) — every route the
  studio exposes.
* [Comment lifecycle and archive rules](/reference/comment_lifecycle.md) —
  the canonical state and archive model.
* [`okf-loom.config.yaml` reference](/reference/config_yaml.md) — the
  `studio.*` defaults and how to change them.
* [`/reference/spec.md`](/reference/spec.md) — the binding current
  specification this essay summarises.

Return to [Explanation](/explanation/index.md).
