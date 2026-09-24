---
type: Table
title: Customers
description: One row per registered customer, with their lifetime value and contact info.
resource: https://warehouse.example.com/tables/customers
tags: [customers, pii]
timestamp: "2026-06-15T08:00:00Z"
entities:
  - label: Salesforce CRM
    kind: system
    source: https://salesforce.example.com
provenance:
  source: warehouse
  imported_at: "2026-06-01T00:00:00Z"
---

# Schema

| Column | Type | Description |
|---|---|---|
| `customer_id` | STRING | Globally unique customer identifier. |
| `email` | STRING | Verified email address (PII). |
| `first_seen_at` | TIMESTAMP | When the customer first appeared. |
| `lifetime_value_usd` | NUMERIC | Cumulative spend to date. |
| `tier` | STRING | Customer SLA tier (see [SLA tiers](/references/sla_tiers.md)). |

# Notes

Email is considered PII. Join on `customer_id` from
[orders](/tables/orders.md). Customer records are synced nightly from
Salesforce CRM. A customer's `lifetime_value_usd` is recomputed by the
[monthly revenue](/metrics/monthly_revenue.md) pipeline.
