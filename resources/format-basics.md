# OKF format basics

## Bundle

A bundle is a directory tree of `.md` files:

```text
my_bundle/
├── index.md
├── log.md
├── datasets/
│   └── sales.md
└── tables/
    ├── orders.md
    └── customers.md
```

## Concept file

```markdown
---
type: Table
title: Customer Orders
description: One row per completed customer order.
resource: https://example.com/orders
tags: [sales, orders]
---

# Schema

| Column | Type | Description |
|---|---|---|
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
```

## Frontmatter contract

| Key | Status | Notes |
|---|---|---|
| `type` | required | Short kind string. Consumers tolerate unknown types. |
| `title` | recommended | Human display name; filename fallback exists. |
| `description` | recommended | One-line summary for indexes/search. |
| `resource` | recommended | Canonical URI for an underlying asset. |
| `tags` | recommended | YAML list of cross-cutting labels. |
| `generated` | optional | Actual producer `by`, optional aware ISO 8601 `at`. Never infer provenance. |
| `sources` | optional | Resource/scope entries with stable ids for claim footnotes. |
| `verified` | optional | Actual `{by, at}` checks, list or bare singleton; advisory trust only. |
| `status` / `stale_after` | optional | Absent status is stable; stale on/after aware expiry. |
| `timestamp` | legacy | Fallback only when `generated` is absent. |

Producers may add arbitrary keys. Consumers and mutators must preserve unknown
keys round-trip.

No optional family is needed for conformance. See the
[frontmatter reference](../docs-bundle/reference/frontmatter.md) for field shapes
and metadata-only Attested Computation support. Do not invent humans, generation
times, or verification when copying a template or importing existing text.

## Link forms

| Form | Example | Guidance |
|---|---|---|
| Absolute bundle-relative | `[customers](/tables/customers.md)` | Default; stable when files move within a subdirectory. |
| Relative | `[next](./other.md)` | Fine for tightly-coupled siblings. |
| Wikilink body syntax | `[[tables/customers\|Customer]]` | Accepted migration syntax; mutators write standard Markdown links. |

Broken internal links are warnings by default for consumers because they may
point to not-yet-written knowledge. Authoring mutators fail closed on missing
targets unless forward references are explicitly allowed.
