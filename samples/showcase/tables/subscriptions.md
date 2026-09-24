---
type: Table
title: Subscriptions
description: One row per active subscription; one row per status transition.
resource: https://warehouse.example.com/tables/subscriptions
tags: [subscriptions, billing]
timestamp: "2026-06-15T08:00:00Z"
---

# Schema

| Column | Type | Description |
|---|---|---|
| `subscription_id` | STRING | Globally unique subscription identifier. |
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
| `plan` | STRING | `monthly`, `quarterly`, `annual`. |
| `started_at` | TIMESTAMP | When the subscription began. |
| `renewed_at` | TIMESTAMP | Last successful renewal. |
| `status` | STRING | `active`, `past_due`, `cancelled`. |

# Notes

Subscriptions are managed by the [billing service](/services/billing.md).
A subscription feeds the [orders](/tables/orders.md) table on each renewal.
