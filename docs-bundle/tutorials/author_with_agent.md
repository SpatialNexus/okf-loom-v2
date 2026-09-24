---
type: Tutorial
title: Direct the agent loop
description: Drive the canonical OKF agent loop — wait for a comment, claim it, author via mutators, resolve it, and loop.
resource: /tutorials/author_with_agent.md
tags: [studio, agent-loop, comments, mutators]
timestamp: "2026-06-29T00:00:00Z"
---

# Direct the agent loop

This tutorial walks you through the canonical OKF agent loop: block on
the next open comment, claim it, author the change through the CLI
mutators, resolve the comment, and loop.

In the OKF studio the human **directs** by posting comments, and the
agent **authors** the markdown files through the mutator commands.
This tutorial shows the agent side of that split — the commands that
turn a comment into an applied, undoable edit.

This tutorial assumes you have finished
[Use the live studio](/tutorials/live_studio_basics.md) and have a
bundle served with at least one open comment waiting.

# What you will have at the end

By the end of this tutorial you will have:

- Run `scripts/okf-loom wait` as a foreground blocking call.
- Claimed a comment and announced editing presence.
- Authored a change through the mutator verbs with a shared group id.
- Resolved the comment with a reply and activity links.
- Seen the full loop and know how to repeat it.

You will be able to drive the comment-to-resolve loop on your own.

# The loop at a glance

The canonical loop is five moves, repeated:

```text
scripts/okf-loom wait        →  scripts/okf-loom comment-claim  →  mutators  →  scripts/okf-loom comment-resolve  →  loop
```

| Move | Command | What it does |
|---|---|---|
| Wait | `scripts/okf-loom wait <bundle> --for comment` | Blocks until an open comment exists, then prints it as JSON and exits. |
| Claim | `scripts/okf-loom comment-claim <bundle> <id> --summary "…"` | Marks the comment `claimed` and flips presence to editing. |
| Author | `scripts/okf-loom link-add` / `scripts/okf-loom entity-add` / `scripts/okf-loom write-concept` | Writes the change to the bundle files. |
| Resolve | `scripts/okf-loom comment-resolve <bundle> <id> --reply "…" --activity … --summary "…"` | Marks the comment `resolved` and links the change-list entries. |
| Loop | `scripts/okf-loom wait …` again | Block on the next comment. |

The full flag surface for each command is in
[CLI command reference](/reference/cli.md).

# A note on scripts/okf-loom wait — read this before you run anything

`scripts/okf-loom wait` is a **foreground, block-once** primitive.
It blocks until work arrives, prints exactly one work item as JSON, and
exits.
You then act on that item and call `scripts/okf-loom wait` again.

**Never background it.**
Do not run `scripts/okf-loom wait &`, do not wrap it in `nohup`, and do not detach it
into a subprocess you do not immediately await.

A backgrounded `scripts/okf-loom wait` produces a work item that nothing consumes.
The comment goes `claimed` (or sits `open`), the user sees no progress,
and the loop silently stalls.
The correct shape is block → act → block, with you as the processor.

If you need a continuous tail for a long-running consumer (a different
shape), use `scripts/okf-loom watch` instead — but for driving the comment loop,
`scripts/okf-loom wait` is the right tool, run in your foreground.

# Step 1: Bring up the studio

The mutators detect a live studio session and route every write through
it, which is what gives you attribution, undo snapshots, and live
updates.
So start the studio first.

In one terminal:

```bash
scripts/okf-loom serve ~/my-first-bundle --no-open
```

Leave it running.
It writes a per-session token to `.okf-loom/session/.token` that the CLI
mutators read automatically — you do not handle the token yourself.

# Step 2: Post a comment for the agent

From the studio's Comments panel (or a second terminal using a helper
script), post a concrete directive, for example:

```text
Add a typed depends_on link from tables/orders to services/checkout, and add an Order entity to tables/orders.
```

Confirm it is waiting:

```bash
scripts/okf-loom comment-list ~/my-first-bundle --state open
```

Note the comment id the list returns — you will use it in the next steps.

# Step 3: Wait for the comment

In your agent terminal, block on the next open comment:

```bash
scripts/okf-loom wait ~/my-first-bundle --for comment
```

The command blocks until an open comment exists, then prints it as JSON
and exits `0`:

```json
{"id": "01JABED9K0", "concept": "tables/orders", "body": "Add a typed depends_on link ...", "state": "open"}
```

Copy the `id` from that output.
If no comment is open and you set a `--timeout`, the command exits `1`
after the timeout — that is the signal to loop back or stop.

`scripts/okf-loom wait` drains the backlog first: without `--since`, the first call
returns the oldest pre-existing open comment immediately, then subsequent
calls drain the rest before settling into wait-for-new-work.

# Step 4: Claim the comment

Claim the comment by its id and post a short summary that will show in
the collapsed thread preview:

```bash
scripts/okf-loom comment-claim ~/my-first-bundle 01JABED9K0 \
  --summary "adding depends_on link + Order entity"
```

Claiming does two things at once:

- It marks the comment `claimed` so no other consumer picks it up.
- It auto-flips the agent's presence to `editing` on the comment's
  concept, so the user immediately sees you are on it.

You do not need a separate `presence` call after a claim.
The user's Comments panel now shows your summary chip on the collapsed
thread.

# Step 5: Author via the mutators

Write the change through the mutator verbs.
Pass the same `--group-id` to every write in this pass so the whole
change is one-click group-undoable for the user:

```bash
scripts/okf-loom link-add --bundle ~/my-first-bundle \
  --source tables/orders --target services/checkout \
  --relation depends_on \
  --evidence "orders are appended by the checkout service" \
  --group-id PASS1
```

```bash
scripts/okf-loom entity-add --bundle ~/my-first-bundle \
  --id tables/orders --label "Order" --kind business_entity \
  --entity-id entity/order --alias "Sales Order" \
  --group-id PASS1
```

Because a studio session is live, each mutator routes through the
studio's save funnel.
That means every write is:

- **Attributed** — recorded with `actor=agent` and `origin=mutator`.
- **Undoable** — a snapshot lands under `.okf-loom/session/history/`.
- **Visible** — a row is appended to `.okf-loom/session/events.jsonl` and a
  `changed` (plus `graph`, for link-affecting ops) event is pushed over
  the live channel.

Each command prints the activity id of the write it just made.
Collect those ids — you will link them from the resolution in the next
step.

# Step 6: Resolve the comment

Resolve the comment, linking the activity ids from the pass and writing
both a long-form reply and a short summary:

```bash
scripts/okf-loom comment-resolve ~/my-first-bundle 01JABED9K0 \
  --activity 01JABED5K7,01JABED5K8 \
  --reply "Done — added the depends_on link and the Order entity." \
  --summary "done: link + entity added"
```

The `--activity` ids are what make the pass one-click group-undoable from
the change list: a single undo reverts both writes together.
The `--reply` is the long-form resolution message; the `--summary` is
the one-line blurb shown in the collapsed thread preview.

After this command the comment is `resolved`, the user sees your reply,
and the change-list entries are linked to the resolution.

# Step 7: Loop

Call `scripts/okf-loom wait` again to block on the next comment:

```bash
scripts/okf-loom wait ~/my-first-bundle --for comment
```

That is the whole loop.
Every iteration is the same five moves: wait, claim, author, resolve,
repeat.

If the wait times out or you are done for the session, set presence back
to idle so the user knows you have stepped away:

```bash
scripts/okf-loom presence ~/my-first-bundle --state idle
```

# A worked four-step example

Here is the loop end to end for the directive in Step 2, condensed into
four copy-pasteable moves.
Assume the comment id is `01JABED9K0`.

```bash
# 1. Wait for the next open comment (foreground — do not background this).
scripts/okf-loom wait ~/my-first-bundle --for comment

# 2. Claim it and announce what you are doing.
scripts/okf-loom comment-claim ~/my-first-bundle 01JABED9K0 \
  --summary "adding depends_on link + Order entity"

# 3. Author the change as one undoable group.
scripts/okf-loom link-add --bundle ~/my-first-bundle \
  --source tables/orders --target services/checkout \
  --relation depends_on \
  --evidence "orders are appended by the checkout service" \
  --group-id PASS1
scripts/okf-loom entity-add --bundle ~/my-first-bundle \
  --id tables/orders --label "Order" --kind business_entity \
  --entity-id entity/order --alias "Sales Order" \
  --group-id PASS1

# 4. Resolve the comment, linking the activity ids from step 3.
scripts/okf-loom comment-resolve ~/my-first-bundle 01JABED9K0 \
  --activity 01JABED5K7,01JABED5K8 \
  --reply "Done — added the depends_on link and the Order entity." \
  --summary "done: link + entity added"
```

After step 4, call `scripts/okf-loom wait` again and the loop continues.

# Where to go next

You can now drive the comment-to-resolve loop on your own.

When threads accumulate or you need to triage them:

* [Archive and resolve comment threads](/how-to/archive_threads.md) — the archive and resolution rules.
* [Find and fix bundle gaps](/how-to/discover_and_fix_gaps.md) — feed discovery suggestions into the same mutators.

The machinery behind every command above is documented in:

* [CLI command reference](/reference/cli.md) — every flag on `wait`, `comment-claim`, `comment-resolve`, and the mutators.
* [Comment lifecycle and archive rules](/reference/comment_lifecycle.md) — the open → claimed → resolved state machine and the archive track.

For the design rationale behind the foreground-wait contract and the
comment-driven model, see
[Why the live studio exists](/explanation/live_studio_design.md).

Return to [Tutorials](/tutorials/index.md).
