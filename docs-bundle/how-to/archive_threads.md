---
type: How-to
title: Archive and resolve comment threads
description: Archive and unarchive comment threads in the live studio using the current archive track, where archived is a separate boolean, archiving is root-only and gated on whole-thread-resolved, and replying auto-unarchives.
resource: /how-to/archive_threads.md
tags: [comments, archive, studio, lifecycle]
timestamp: "2026-06-29T00:00:00Z"
---

# Archive and resolve comment threads

This guide walks you through clearing the studio's comment panel by
archiving resolved threads, and bringing them back when work resumes.

You already know how to drive the comment loop end to end.
If you do not, [Direct the agent loop](/tutorials/author_with_agent.md)
covers the `wait`, claim, and resolve flow.

# The archive model (read this first)

Archive is a **separate boolean field** next to
`state`:

| Field | Values | Meaning |
|---|---|---|
| `state` | `open` / `claimed` / `resolved` / `dismissed` | The lifecycle of the work. |
| `archived` | `true` / `false` | Whether the panel soft-hides the thread. |

Archiving a resolved comment keeps it resolved.
Unarchiving restores the prior visibility without flipping `state`.
Compatibility records with `state == "archived"` are normalised on read to
`archived=true, state="open"` (best-effort migration, automatic).

# Step 1: Resolve every comment in the thread

Archive is server-enforced gated on the **whole** thread being resolved.
The agent resolves its comments via the CLI; the user resolves theirs via
the panel.

```bash
scripts/okf-loom comment-resolve path/to/bundle 01JABED9K0 \
  --activity 01JABED5K7,01JABED5K8 \
  --reply "Done — added depends_on + Order entity" \
  --summary "done: link + entity added"
```

If the thread has replies, every reply must also be resolved before the
root can be archived.

# Step 2: Archive the thread from the panel

In the studio's right-hand **Comments** panel, open the thread you want
to clear.

On the root card, click **Archive thread**.

The button is only enabled when every comment in the thread is
`resolved`.
Before that, it is disabled with a tooltip explaining the gate.

The thread disappears from the default panel view; its lifecycle state is
untouched.

Toggle **Show archived** in the panel toolbar to see archived threads
again.

# Step 3: Archive the thread via the HTTP endpoint

If you are driving the studio from a script, POST to
`/__comment-update` with the root id and the archive flag.

```bash
TOKEN=$(scripts/okf-loom token path/to/bundle)

curl -s -X POST http://127.0.0.1:8787/__comment-update \
  -H "Content-Type: application/json" \
  -H "X-OKF-Token: $TOKEN" \
  -d '{"id": "01JABED9K0", "expected_row_token": "r2:01K0ROW8N7Q2", "archived": true}'
```

A successful response returns the updated comment object with
`archived: true`.

# Step 4: Handle the archive guards

The server enforces three rules.

| Rule | Status | Body |
|---|---|---|
| Archive is root-only. Replies cannot be archived independently. | `400` | `{"error": "archive is only available on the top-level comment of a thread"}` |
| Archive requires every comment in the thread to be `resolved`. | `409` | `{"error": "...", "unresolved_ids": ["01J...", "01J..."]}` |
| Dismiss is rejected on a `claimed` comment (agent is mid-flight). | `409` | `{"error": "cannot dismiss a claimed comment"}` |

When you get a `409` with `unresolved_ids`, resolve each listed comment
and retry.

# Step 5: Unarchive a thread

Unarchive from the panel by clicking **Unarchive** on the archived root
card (visible with **Show archived** on).

Or POST the same endpoint with `archived: false`:

```bash
curl -s -X POST http://127.0.0.1:8787/__comment-update \
  -H "Content-Type: application/json" \
  -H "X-OKF-Token: $TOKEN" \
  -d '{"id": "01JABED9K0", "expected_row_token": "r2:01K0LATEST8N7Q2", "archived": false}'
```

Unarchiving restores the prior visibility without changing `state`.
A resolved thread comes back resolved; an open thread comes back open.

# Step 6: Let a reply auto-unarchive

Replying to an archived thread **auto-unarchives the parent**.

This is enforced server-side in `Studio.post_comment`: the moment you add
to a thread, it reopens for business.
Archive is a soft hide, not a closed state.

In the panel, clicking **Reply** on an archived thread and submitting a
reply both unarchives the thread and posts the reply.

# Step 7: List comments with the archive flag

`comment-list` shows every directive and each row
carries `archived: bool` independent of state.

```bash
scripts/okf-loom comment-list path/to/bundle
```

Filter by state with `--state open|claimed|resolved|dismissed`; the
archive flag is an independent column you read alongside it.

# End state

You can now keep the comment panel focused on open work by archiving
resolved threads, while keeping them one click away when work resumes.

# See also

* [Direct the agent loop](/tutorials/author_with_agent.md) — the full claim → write → resolve loop.
* [Comment lifecycle and archive rules](/reference/comment_lifecycle.md) — the canonical state + archive reference.
* [HTTP server routes](/reference/http_routes.md) — every studio endpoint.
* [CLI command reference](/reference/cli.md) — `comment-list`, `comment-claim`, `comment-resolve`, `token`.
* [Why the live studio exists](/explanation/live_studio_design.md) — why comments are the user's directing surface.

Return to [How-to guides](/how-to/index.md).
