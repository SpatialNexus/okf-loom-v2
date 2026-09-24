---
type: Table
title: Orders
description: One row per completed order, denormalised for analytics.
resource: https://warehouse.example.com/tables/orders
tags: [orders,事实]
timestamp: "2026-06-15T08:00:00Z"
relations:
  - type: derived_from
    target: /tables/subscriptions.md
---

# Schema

| Column | Type | Description |
|---|---|---|
| `order_id` | STRING | Globally unique order identifier. |
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
| `subscription_id` | STRING | Optional FK to [subscriptions](/tables/subscriptions.md). |
| `placed_at` | TIMESTAMP | When the order was placed. |
| `gross_amount_usd` | NUMERIC | Gross amount before refunds. |
| `currency` | STRING | ISO 4217 code (see [currencies](/references/currencies.md)). |
| `status` | STRING | `placed`, `fulfilled`, `refunded`. |

# Notes

oh yes - we update :)

so update

Orders are written by the [checkout service](/services/checkout.md) on every
completed checkout. Each write is idempotent on `order_id`, so retries from
the checkout pipeline never produce duplicate rows; the checkout service is
the sole producer of this table.

For analytics and downstream consumption, completed orders are mirrored to
the warehouse hourly. The mirror is append-only and partitions by `placed_at`
date, which keeps historical reporting stable even when a later
[refund](/playbooks/refund_flow.md) adjusts `gross_amount_usd`.

The order stream is event-sourced per
[ADR: event sourcing](/decisions/adopt_event_sourcing.md): every state
transition (`placed` → `fulfilled` → `refunded`) is an immutable event, and
the row in this table is a materialised projection of that event log. That
makes point-in-time reconstruction and replays straightforward.

This notes section was expanded into four paragraphs from the original single
paragraph; the expansion was requested by B.
