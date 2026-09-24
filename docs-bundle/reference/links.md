---
type: Reference
title: Link forms
description: The OKF 0.2 link forms (upstream §6) — absolute bundle-relative and relative — plus source links, anchors, external links, Loom wikilinks, and broken-link tolerance.
resource: /reference/links.md
tags: [links, graph, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# Link forms

Concepts cross-link with standard markdown links. The link graph is
what turns a tree of files into a knowledge graph: links are the
edges, concepts are the nodes, and the
[graph view](cli.md#graph) + [backlinks panel](capabilities.md) are
the navigable surfaces built on top.

OKF supports two link forms (upstream 0.2 §6.1). Both are valid; okf-loom
handles both in its validator, viewer, search, and graph.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

# The two forms

| Form | Syntax | SPEC | Stability |
|---|---|---|---|
| **Absolute bundle-relative** (recommended) | `[label](/tables/customers.md)` | §6.1 | Stable when the linking document moves within a subdirectory. |
| **Relative** | `[label](./customers.md)` or `[label](../parent/thing.md)` | §6.1 | Stable only for tightly-coupled siblings that always move together. |

Both forms resolve to the same target concept id; the graph treats
them identically.

okf-loom fixes an upstream bug where the reference viewer silently
dropped absolute links, producing incomplete graphs. Absolute is the
recommended form for new authoring.

## Absolute bundle-relative (upstream §6.1)

A leading `/` makes the path bundle-relative. The path is the concept
file's path from the bundle root.

```markdown
The order's customer is in [customers](/tables/customers.md).
See also the [checkout service](/services/checkout.md).
```

Use this form by default. It stays correct when a file moves within
its subdirectory (e.g. `tables/customers.md` → `tables/v2/customers.md`
would need a path update, but `tables/orders.md` → `tables/v2/orders.md`
leaves the link from `services/checkout.md` intact).

## Relative (upstream §6.1)

No leading `/`; resolves against the linking file's directory.

```markdown
The next concept is [orders](./orders.md).
The parent is [tables](../index.md).
```

Use only for tightly-coupled siblings that always move together.

# Anchors

OKF 0.2 `sources[].resource` can also identify internal concepts, contributing
source relationships. A population/scope descriptor is text, not a guessed link.
Stable source ids join claim footnotes independently of graph paths; see the
[frontmatter contract](/reference/frontmatter.md).

Both forms support same-document and cross-document anchors:

```markdown
# Same-document anchor
See the [schema section](#schema).

# Cross-document anchor
FK to [customers](/tables/customers.md#schema).
```

Anchors are derived from heading text via slugify. They are not
graph edges; they are intra-document or intra-concept navigation.

# External links

Links whose scheme is `http`, `https`, `mailto`, etc. are external.
okf-loom keeps them, classifies them as external, and does NOT
add them as graph edges.

```markdown
Spec: [current okf-loom spec](/reference/spec.md).
Contact: [data team](mailto:data@example.com).
```

The validator emits an INFO finding (`link.external`) for each
external link so they are countable but not promoted to errors.

# Wikilinks (Loom extension)

Obsidian-style wikilinks are recognised as body syntax (no
frontmatter key required). They auto-activate the
`okf.cap.wikilinks` capability (see [capabilities.md](capabilities.md)).

```markdown
[[tables/customers]]
[[tables/customers|Customer]]
```

The link extractor resolves wikilinks to concept ids and emits them
as graph edges with `form="wikilink"` (visible in
`graph --format json` and the viewer). The validator emits an
INFO finding (`link.wikilink_present`) suggesting conversion to
standard markdown links for portability.

The mutator verbs ([`write-concept`](cli.md#write-concept),
[`link-add`](cli.md#link-add), etc.) always emit standard markdown
links, never wikilinks. The recommended authoring form is therefore
absolute bundle-relative markdown links; wikilinks are tolerated for
Obsidian/Quartz interop.

# Broken-link tolerance (upstream §6.1)

A link whose target does not exist is **not malformed**. It may be
not-yet-written knowledge. okf-loom surfaces broken links as
`WARNING` findings (`link.broken`) by default; explicit strict/broken-link policy
can fail validation and authoring mutators reject missing targets by default.

```markdown
We will add [returns](/tables/returns.md) soon — the link is valid
OKF even though `tables/returns.md` does not yet exist.
```

This is the upstream §6.1 consumer contract: links may point to forthcoming
concepts. The link becomes a real edge the moment you create the
target file.

## Forward references

[`scripts/okf-loom link-add --allow-forward-reference`](cli.md#link-add) writes a
link whose target does not yet exist. The link is written in the
absolute form and is `broken` (WARNING) until the target concept is
created.

# Code blocks and inline code

Markdown links inside fenced code blocks or inline code are NOT
counted as graph edges. The viewer will not navigate them.

```markdown
The literal path `/tables/users.md` is shown to the reader but is
not a graph edge because it is in inline code.
```

# Where to put links

In the **body prose**, where the relationship is asserted. A common
pattern is the `# Schema` table where foreign-key columns link to
their target concept:

```markdown
# Schema

| Column | Type | Description |
|---|---|---|
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
| `product_id` | STRING | FK to [products](/tables/products.md). |

# Joins

Joined with [customers](/tables/customers.md) on `customer_id` to
enrich orders with user geography.
```

The discovery pass ([`scripts/okf-loom discover --rules unlinked_mentions`](cli.md#discover))
flags mentions of a concept's title that appear in body prose without
a link; those are candidates for `scripts/okf-loom link-add`.

# How the viewer routes links

The live studio ([`scripts/okf-loom serve`](cli.md#serve)) normalises both link
forms to the same canonical route:

| Link | Route |
|---|---|
| `/tables/customers.md` | `/tables/customers` |
| `/tables/customers` | `/tables/customers` |
| `./customers.md` (from `tables/orders.md`) | `/tables/customers` |

The route layer strips a trailing `.md` (except for the reserved
`/index.md` routes) and a trailing slash (except for root).
See [http_routes.md](http_routes.md).

# Choosing a form

| Situation | Use |
|---|---|
| Default for new authoring | Absolute (`/tables/customers.md`). |
| Tightly-coupled siblings (always move together) | Relative (`./customers.md`). |
| Same-document navigation | Anchor (`#schema`). |
| Cross-document section navigation | Absolute + anchor (`/tables/customers.md#schema`). |
| External asset | External (`https://…`, `mailto:…`). |
| Obsidian interop | Wikilink (`[[tables/customers]]`); convert to markdown when convenient. |
| Not-yet-written target | Absolute forward reference; tolerate the WARNING. |

# Validation

| Check | Severity | When it fires |
|---|---|---|
| `link.broken` | WARNING (or ERROR with `--fail-on-broken-links`) | Target concept does not exist. |
| `link.external` | INFO | Link has an external scheme. |
| `link.wikilink_present` | INFO | Body contains a `[[…]]` wikilink. |

```bash
scripts/okf-loom validate path/to/bundle --checks link_integrity
scripts/okf-loom validate path/to/bundle --fail-on-broken-links
scripts/okf-loom graph path/to/bundle --format json   # inspect edges + forms
```

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [cli.md](cli.md) — `link-add`, `graph`, `validate`, `discover`.
- [frontmatter.md](frontmatter.md) — frontmatter contract (links live in the body, not frontmatter).
- [capabilities.md](capabilities.md) — `okf.cap.links`, `okf.cap.wikilinks`, `okf.cap.backlinks`.
- [http_routes.md](http_routes.md) — how the viewer routes links.
- [/how-to/author_with_verbs.md](/how-to/author_with_verbs.md) — adding links via `scripts/okf-loom link-add`.
- [/how-to/discover_and_fix_gaps.md](/how-to/discover_and_fix_gaps.md) — finding unlinked mentions.
- [/tutorials/first_bundle.md](/tutorials/first_bundle.md) — first links in a new bundle.
- [/reference/spec.md](/reference/spec.md) — current okf-loom link contract.
- Upstream OKF 0.2 §6 — base wire-format link contract.
