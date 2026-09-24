---
type: Table
title: Orders
description: One row per completed customer order.
resource: https://warehouse.example.com/tables/orders
tags: [orders, revenue]
timestamp: "2026-06-27T09:30:00Z"
aliases: [purchase orders, sales orders, Aufträge]
entities:
  - id: entity/order
    label: Order
    kind: business_entity
    aliases: [purchase, transaction]
relations:
  - target: tables/customers
    type: references
    detail: orders.customer_id -> customers.customer_id
  - target: services/checkout
    type: written_by
    detail: checkout appends rows on order completion
  - target: datasets/orders
    type: belongs_to
    detail: ""
provenance:
  - source: https://wiki.example.com/orders
    note: imported from internal data dictionary
    timestamp: "2026-06-27T00:00:00Z"
citations:
  - id: "1"
    text: "Internal data dictionary"
    url: https://wiki.example.com/data/orders
---

# Schema

| Column | Type | Description |
|---|---|---|
| `order_id` | STRING | Globally unique order identifier. |
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
| `total_usd` | NUMERIC | Order total in US dollars. |
| `currency` | STRING | ISO 4217 currency code; see [currencies](/references/currencies.md). |
| `placed_at` | TIMESTAMP | When the order was submitted. |

# Examples

```sql
SELECT
  DATE(placed_at) AS day,
  COUNT(*) AS orders,
  SUM(total_usd) AS gross_revenue
FROM orders
WHERE placed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY day
ORDER BY day DESC;
```

# Notes

The `customer_id` foreign key points at [[tables/customers|Customers]], and
each row is materialised into the [orders dataset](/datasets/orders.md) for
downstream analytics. Wikilinks like `[[tables/customers]]` are SPEC §10
body syntax; okf-loom resolves them to the same concept id as a standard
markdown link and emits them with `form="wikilink"` in the graph.

# Citations

- Internal data dictionary: https://wiki.example.com/data/orders
