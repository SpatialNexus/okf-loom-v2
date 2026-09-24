---
type: How-to
title: Author concepts via CLI verbs
description: Edit OKF concepts from the command line with write-concept, set-frontmatter, link-add, and entity-add, grouping writes for one-click undo and attributing them to an actor.
resource: /how-to/author_with_verbs.md
tags: [authoring, verbs, mutators, curation]
timestamp: "2026-06-29T00:00:00Z"
---

# Author concepts via CLI verbs

This guide shows you how to edit concept files from the command line
without hand-writing a JSON update plan.

You already know what a concept looks like and have run validation at
least once.
If you do not, [Author your first bundle](/tutorials/first_bundle.md)
walks you through the basics.

# Step 1: Create or update a concept

`write-concept` creates a concept if it is missing, or merges
frontmatter into an existing one.

```bash
scripts/okf-loom write-concept --bundle path/to/bundle \
  --id tables/orders \
  --type "Table" \
  --title "Orders" \
  --description "One row per completed order." \
  --tag analytics --tag checkout \
  --body-file ./orders_body.md
```

Flags worth knowing:

| Flag | Effect |
|---|---|
| `--type` | Required (upstream OKF 0.2 §11: `type` is the only always-required key). |
| `--title` / `--description` / `--resource` / `--timestamp` | Set the corresponding frontmatter key. |
| `--tag` | Repeatable; appends to `tags`. |
| `--body` / `--body-file` | Set the body. Replacing a non-empty body requires `--force`. |
| `--force` | Allow overwriting an existing body. |

Updating an existing concept only touches the keys you pass; unknown keys
are preserved.

# Step 2: Set a single frontmatter key

For a one-key edit, `set-frontmatter` is thinner than `write-concept`.

```bash
scripts/okf-loom set-frontmatter --bundle path/to/bundle \
  --id tables/orders \
  --key owner --value team-checkout
```

For lists or objects, pass `--json-value`:

```bash
scripts/okf-loom set-frontmatter --bundle path/to/bundle \
  --id tables/orders \
  --key tags --value '["analytics","checkout"]' --json-value
```

`--dry-run` previews the change without writing.

# Step 3: Add a link (and optionally a typed relation)

`link-add` appends a markdown link in the absolute
`/tables/customers.md` form recommended by upstream OKF 0.2 §6.1.

```bash
# Plain body link
scripts/okf-loom link-add --bundle path/to/bundle \
  --source tables/orders --target tables/customers \
  --label "customers" --section "Schema"
```

Add `--relation TYPE` to also append a typed relation; `--evidence`
becomes the relation `detail`.

```bash
scripts/okf-loom link-add --bundle path/to/bundle \
  --source tables/orders --target services/checkout \
  --relation written_by --evidence "POST handler in checkout service"
```

By default the verb refuses to write a link whose target does not exist.
Pass `--allow-forward-reference` to write a deliberate forward reference
(upstream §6.1 tolerates it; see [Link forms](/reference/links.md)).

# Step 4: Add an entity

`entity-add` appends an entity object to the concept's `entities:`
frontmatter list.

```bash
scripts/okf-loom entity-add --bundle path/to/bundle \
  --id tables/orders \
  --label "Order" --kind business_entity \
  --entity-id entity/order \
  --alias "Sales Order" --alias "Order Record"
```

It is idempotent on `(label, kind)`, so re-running the same command does
not duplicate the entity.
Aliases are repeatable; they also feed entity and semantic search once
present.

# Step 5: Group writes for one-click undo

Pass the same `--group-id` to every verb in a logical pass and the studio
collapses them into one undoable change.

```bash
scripts/okf-loom link-add --bundle path/to/bundle \
  --source tables/orders --target tables/customers \
  --relation depends_on --group-id PASS1

scripts/okf-loom entity-add --bundle path/to/bundle \
  --id tables/orders --label "Order" --kind Table \
  --group-id PASS1
```

In the change list, `PASS1` appears as a single entry with a one-click
group-undo button.
Without a live studio session the flag is accepted but has no visible
effect — the writes still land atomically.

# Step 6: Attribute writes to an actor

`--actor NAME` sets the attribution shown in the change list (default
`agent`).

```bash
scripts/okf-loom link-add --bundle path/to/bundle \
  --source tables/orders --target tables/customers \
  --group-id PASS1 --actor curator-alice
```

Use this when a human or a named workflow is doing the curation, so the
change list reads as a real activity log.

# Step 7: Run a worked multi-verb curation pass

This sequence is the typical "wire up one concept end to end" pass you
will run dozens of times.

```bash
BUNDLE=path/to/bundle
GROUP=curate-orders-$(date +%s)

# 1. Make sure the concept exists with rich metadata
scripts/okf-loom write-concept --bundle "$BUNDLE" \
  --id tables/orders \
  --type "Table" \
  --title "Orders" \
  --description "One row per completed customer order." \
  --tag analytics --tag checkout \
  --group-id "$GROUP" --actor curator-alice

# 2. Backfill an owner key
scripts/okf-loom set-frontmatter --bundle "$BUNDLE" \
  --id tables/orders \
  --key owner --value team-checkout \
  --group-id "$GROUP" --actor curator-alice

# 3. Link to the customer table + add a typed relation
scripts/okf-loom link-add --bundle "$BUNDLE" \
  --source tables/orders --target tables/customers \
  --label "customers" --section "Schema" \
  --relation depends_on --evidence "FK customer_id" \
  --group-id "$GROUP" --actor curator-alice

# 4. Register the Order entity with aliases
scripts/okf-loom entity-add --bundle "$BUNDLE" \
  --id tables/orders \
  --label "Order" --kind business_entity \
  --entity-id entity/order \
  --alias "Sales Order" --alias "Order Record" \
  --group-id "$GROUP" --actor curator-alice

# 5. Re-validate
scripts/okf-loom validate "$BUNDLE"
```

Every write in that pass shares `"$GROUP"`, so the whole curation is a
single undo in the studio.

# When to use the verbs vs a plan

Use the verbs for one-off edits while authoring: create a concept, fix a
frontmatter typo, wire up a single link or entity.

Use `plan` plus `update` for batch curation produced by
`discover` — many ops on many concepts, reviewable as one JSON
document.

Both paths share the same `parse_document` → modify → `serialize_document`
pipeline, so file-level behaviour is identical.

See [Find and fix bundle gaps](/how-to/discover_and_fix_gaps.md) for the
batch path.

# See also

* [Find and fix bundle gaps](/how-to/discover_and_fix_gaps.md) — turn discovery suggestions into a reviewable plan.
* [Direct the agent loop](/tutorials/author_with_agent.md) — the same verbs inside the comment-driven studio loop.
* [CLI command reference](/reference/cli.md) — every flag on every verb.
* [Frontmatter contract](/reference/frontmatter.md) — required vs recommended keys.
* [Why the live studio exists](/explanation/live_studio_design.md) — why mutators attribute and undo.

Return to [How-to guides](/how-to/index.md).
