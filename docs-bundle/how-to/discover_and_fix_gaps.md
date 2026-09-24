---
type: How-to
title: Find and fix bundle gaps
description: Run discovery to find missing links, indexes, descriptions, relations, and orphans, then apply fixes directly via mutators or a reviewable update plan.
resource: /how-to/discover_and_fix_gaps.md
tags: [discover, curation, plan, update]
timestamp: "2026-06-29T00:00:00Z"
---

# Find and fix bundle gaps

This guide walks you through finding the curation gaps a strict validate
cannot see — unlinked mentions, missing indexes, thin descriptions — and
turning them into edits on disk.

You already know how to author a concept and run validation.
If you do not, [Author your first bundle](/tutorials/first_bundle.md)
covers the basics.

# Step 1: Run discovery over the whole bundle

`discover` emits a structured report with one row per suggestion.

```bash
scripts/okf-loom discover path/to/bundle --out suggestions.json
```

The `--out` file is a JSON list you can read in an editor or pipe into a
downstream consumer.

Without `--out`, the report prints to stdout in human-readable form.

# Step 2: Read the rule taxonomy

Each suggestion carries a `rule` field drawn from a fixed taxonomy.

| Rule | Fires when… |
|---|---|
| `unlinked_mentions` | A concept body mentions another concept's title but does not link to it. |
| `missing_indexes` | A directory contains concepts but has no `index.md`. |
| `missing_descriptions` | A concept's `description` is empty or missing. |
| `broken_links` | An internal link target does not exist (re-surfaces the validator's findings). |
| `missing_relations_hint` | A `# Schema` column says "FK to" or "references" but has no outgoing link. |
| `orphan_concepts` | A concept has no incoming and no outgoing internal edges. |

The severity on each row tells you whether to fix-now or to triage.

# Step 3: Triage the suggestions

Read the report and decide per row whether to apply, defer, or dismiss.

```json
[
  {
    "rule": "unlinked_mentions",
    "severity": "warning",
    "concept": "tables/orders",
    "target": "tables/customers",
    "message": "mentions 'customers' without a link",
    "action": "add a link from tables/orders to tables/customers"
  }
]
```

A useful triage order is: fix `broken_links` first, then
`missing_descriptions`, then `unlinked_mentions` and
`missing_relations_hint`, and treat `orphan_concepts` as a signal to
merge or cross-link.

`missing_indexes` is mechanical — it always makes sense to add an
`index.md` when a directory has more than a few concepts.

For aliases that are useful search terms but too broad for automatic linking,
prefer object-form aliases with `discoverable: false`:

```yaml
aliases:
  - label: Architecture
    discoverable: false
```

Those aliases remain available to search and the viewer, but `discover` will
not emit unlinked-mention suggestions for them.

For unlinked mentions, check `detail.location_counts` before applying. A match
seen in body prose is stronger than a match seen only in `frontmatter`, `h1`,
`heading`, or `table`; heading-only and table-only suggestions usually need
review rather than automatic linking.

When a phrase or source→target pair is noisy only in this bundle, configure an
editorial suppression instead of adding it to a universal stoplist:

```yaml
discover:
  suppress_phrases:
    - architecture
  suppress_pairs:
    - source: /project-a/product-spec.md
      target: /project-b/product-spec.md
```

These items stay visible in JSON under `suppressed` with reasons such as
`configured_phrase` or `configured_pair`.

For large reports, use the actionability buckets before reading every row:

- `safe_to_apply` — high-confidence or mechanical suggestions.
- `needs_review` — useful but not automatic.
- `suppressed_existing_relation` — already represented by `relations:`.
- `suppressed_generic_label` / `suppressed_cross_cluster` — likely string-match noise.
- `suppressed_configured` — hidden by this bundle's config.
- `low_confidence` — available for broad audits, not default action.

# Step 4: Apply fixes directly via the mutators

For a handful of suggestions, the authoring verbs are the fastest path.

```bash
# unlinked_mentions: add a body link from orders to customers
scripts/okf-loom link-add --bundle path/to/bundle \
  --source tables/orders --target tables/customers \
  --label "customers" --section "Schema"

# missing_relations_hint: add a typed relation too
scripts/okf-loom link-add --bundle path/to/bundle \
  --source tables/orders --target tables/customers \
  --relation depends_on --evidence "FK customer_id"

# missing_descriptions: backfill a one-liner
scripts/okf-loom set-frontmatter --bundle path/to/bundle \
  --id tables/orders \
  --key description --value "One row per completed order."
```

Each verb is idempotent and goes through the same atomic write path as a
plan.

When a live studio session is running, the verbs also flow through
`Studio.save_concept`, so each edit is attributed, undoable, and pushed
live over SSE.

See [Author concepts via CLI verbs](/how-to/author_with_verbs.md) for the
full verb catalogue.

# Step 5: Apply many fixes via an action plan

For batch curation, turn the suggestions into an executable plan and
apply it in one reviewable pass.

```bash
# Build a plan from discovery + index staleness + relation mirroring
scripts/okf-loom plan path/to/bundle --out plan.json

# Preview the changes without writing
scripts/okf-loom update path/to/bundle --plan plan.json --dry-run

# Apply
scripts/okf-loom update path/to/bundle --plan plan.json

# Re-check
scripts/okf-loom validate path/to/bundle
```

`plan` adds two sources beyond raw discovery: stale `index.md`
regeneration and typed-relation mirroring (writing a body markdown link
for each `relations:` entry that lacks one).

The plan is idempotent — re-applying it reports `applied:0, skipped:N`
with reasons like `already_linked` or `section_exists`.

# Step 6: Scope discovery to a subgraph

When you only touched one concept, run a scoped pass instead of rescanning
the whole bundle.

```bash
# Only the named concept
scripts/okf-loom discover path/to/bundle --scope tables/orders --out scoped.json

# The named concept plus its 1-hop neighbours (in + out edges)
scripts/okf-loom discover path/to/bundle --scope tables/orders --neighbors --out scoped.json
```

`plan` accepts the same `--scope` and `--neighbors` flags, so a
per-change enrichment pass is fast.

The watch loop in the live studio uses exactly this scoped shape to
enrich on every save.

# Step 7: Run mechanical repairs in one go

Some suggestions — stale indexes and missing relation mirrors — are
always safe to apply without judgement.

```bash
scripts/okf-loom repair path/to/bundle --apply
```

`--apply --indexes` regenerates only `index.md` files;
`--apply --mirror-relations` only writes the body links for typed
relations.

Relation mirroring runs before index regen so the regenerated indexes
reflect the new links.

# End state

You now have a workflow that:

- Surfaces every curation gap in a structured report.
- Applies fixes either one-by-one through the verbs or in batch through a
  plan.
- Re-runs cheaply on every change via scoped discovery.

# See also

* [Author concepts via CLI verbs](/how-to/author_with_verbs.md) — the per-suggestion verbs used above.
* [Validate a bundle in CI](/how-to/validate_in_ci.md) — strict conformance as a CI gate.
* [CLI command reference](/reference/cli.md) — every flag on `discover`, `plan`, `update`, `repair`.
* [Capability registry](/reference/capabilities.md) — what `okf.cap.typed_relations` and friends unlock.
* [Why the live studio exists](/explanation/live_studio_design.md) — how scoped discovery powers proactive enrichment.

Return to [How-to guides](/how-to/index.md).
