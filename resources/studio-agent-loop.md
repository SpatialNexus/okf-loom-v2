# Live studio agent loop

Work items may carry `document:"path.md"`, including indexes/logs, with an empty
`concept` for reserved files. Do not invent a concept id or offsets. Claim/reply/
resolve still use the comment id; agent replies inherit the target. Preserve
marker-safe indexes, append-only logs, unknown fields and durable recovery.

Public sharing remains read-only by default. The explicit
`--allow-tunnel-comment` flag on `serve --tunnel` or `tunnel <bundle>` grants only
user create/reply, not row updates, content edit, presence, agent or admin powers.
Do not simulate it with the edit token. Real exposure still requires authority;
permission tests can use dedicated loopback ingress. Watching is presence, not
an agent process: the external `wait` → claim → mutators → resolve loop remains.

Portable exports may configure `viewer.review_base_url` as a credential-free
HTTP(S) deployment base. They remain read-only, with no private comments/session
tokens. UI integration reads `okf-review-bootstrap`; absent configuration means
explain that live Studio is required, never fabricate a destination.

`serve` opens the live collaborative studio. The user reads and directs via
comments; the agent writes OKF files through the mutators.

```bash
scripts/okf-loom serve path/to/bundle --no-open &
until scripts/okf-loom token path/to/bundle >/dev/null 2>&1; do sleep 0.2; done

scripts/okf-loom wait path/to/bundle --for comment
scripts/okf-loom comment-claim path/to/bundle 01JABED9K0 --summary "adding dependency link"
scripts/okf-loom presence path/to/bundle --state editing --focus tables/orders
scripts/okf-loom link-add --bundle path/to/bundle --source tables/orders --target tables/customers --group-id PASS1
scripts/okf-loom comment-resolve path/to/bundle 01JABED9K0 --summary "done: link added"
```

When the ask is ambiguous or arrives cut off, do NOT claim-and-guess or ask
out-of-band — reply in the thread and wait for the answer:

```bash
scripts/okf-loom comment-reply path/to/bundle 01JABED9K0 --body "Did you mean orders or order_items?"
scripts/okf-loom wait path/to/bundle --for comment   # the user's answer is new work
```

Important rules:

- Run `wait` in the foreground. It blocks once, prints one work item, exits,
  and then you act. Do not background it. The returned comment includes a
  `queue` field (`{pending, ids}` of other open comments) so you can see
  backlog depth without diffing `comment-list`.
- Read the work item's `anchor`. A `text-quote` anchor is an exact canonical
  source span. `{"kind": "concept"}` is a page-level comment; when it carries
  `detail.selection` (`surface`, `quote`, `reason`), it is a quoted selection:
  the reader selected that text where it could not be pinned exactly (Source
  view, the graph preview, or across blocks) and was told it is quoted in the
  comment, not attached to exact text. Treat the quote as a hint about what
  they meant, never as an offset.
- Wait for `token` readiness after backgrounding `serve` before mutating.
- Mutators route through `Studio.save_concept` when a live session exists,
  making writes attributed, undoable, and visible over SSE.
- Prefer the partial-update mutators for edits inside an existing body:
  `update-section --heading "## X" --body-file frag.md` swaps one section
  (fail-closed on missing/ambiguous headings), `replace-text --old … --new …`
  applies an exact textual patch. Never reconstruct a whole document (or
  stage a copy) to change one block; reserve `write-concept --force` for
  actual full rewrites.
- `--group-id` groups several writes into one undoable pass.
- During a long pass, keep the user informed:
  `presence --state editing --focus <id> --message "linking 3 of 7 tables"` —
  re-post to update the message; it shows live next to the presence chip.
- To share the running studio publicly without a restart:
  `scripts/okf-loom tunnel path/to/bundle` (needs cloudflared; `--stop`
  detaches). Anyone with the link can READ the bundle, but public access is
  read-only by default. `--allow-tunnel-edit` is the dangerous explicit opt-in
  that gives every link holder comment and mutation capability.
- `--public` or non-loopback binds require explicit `--public-ack` or an
  interactive acknowledgement.
- `--no-edit` makes a read-only kiosk: live reads remain on, mutating endpoints
  are disabled.

Full behavior is documented in [`docs-bundle/reference/spec.md`](../docs-bundle/reference/spec.md)
and implemented in [`scripts/okf_loom/studio.py`](../scripts/okf_loom/studio.py)
plus [`scripts/okf_loom/server.py`](../scripts/okf_loom/server.py).

## A durable loop, and what the Studio says about you

The Studio never starts, hosts or supervises an agent. You are an external
process; the only things that make you visible are the things you do.

`wait` semantics you can rely on:

- `scripts/okf-loom wait <bundle> --for comment` blocks in the foreground until
  there is work, prints **one** JSON work item on stdout and exits `0`.
- With `--timeout SECONDS` it exits `1` with no output when nothing arrived.
  That is "no work", not an error: call `wait` again. Without `--timeout` it
  blocks until work arrives.
- On first run it returns pre-existing open comments one at a time, then
  settles into waiting for new work. `--since <id>` skips
  everything up to a feed id you have already handled.
- `--for comment change` also wakes on bundle change events; `--interval` tunes
  the poll period (default 0.5s).
- `wait` publishes nothing. It does not set presence and the server keeps no
  list of waiting agents.

One pass, repeated for as long as the session should be served:

```bash
item="$(scripts/okf-loom wait path/to/bundle --for comment --timeout 600)" || exit 0  # 1 = no work yet: call again
id="$(printf '%s' "$item" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
scripts/okf-loom comment-claim path/to/bundle "$id" --summary "what you are about to do"
scripts/okf-loom presence path/to/bundle --state editing --focus <concept-id> --message "step 1 of 3"
# ...mutators...
scripts/okf-loom comment-resolve path/to/bundle "$id" --summary "done: what changed"
scripts/okf-loom presence path/to/bundle --state idle
```

What the reader sees, and why:

- **Claim early.** `comment-claim` is the durable evidence that an agent picked a
  thread up: the card reads "claimed by agent" and the availability line reads
  "An agent has claimed N comments". Until you claim, reply or resolve, the
  Studio truthfully reports that no agent has acted in the session.
- **Presence is a status flag with a 300s TTL**, not a heartbeat contract. It
  reverts to `idle` when it goes stale, so re-post it during a long pass. The
  reader is told, on the chip and in its accessible description, that it is a
  reported status, not proof that you are running.
- **The Watching toggle in the Studio does not reach you.** A person can mark the
  agent as "watching" from the browser; it writes the same presence flag and
  starts nothing. Never treat that flag as a request, a wake-up or a claim. Your
  only work source is `wait`.
- **A comment is not a live message.** The composer tells the reader a comment
  is saved on the bundle for its readers and agent and does not message a
  running agent; Studio home's quick actions start ordinary comments too. They
  reach you only through `wait`, so a later pickup is expected, not a failure.
- Resolve with a summary; the collapsed thread shows it as the "done" line. If
  you cannot finish, reply in the thread rather than leaving a silent claim.
