---
type: Dataset
title: Orders (analytics)
description: Order-history dataset backing all revenue and retention analytics.
resource: https://warehouse.example.com/datasets/orders
tags: [orders, analytics]
timestamp: "2026-06-15T08:00:00Z"
provenance:
  source: event bus
  pipeline: dbt
---

# Coverage

* **Granularity:** one row per order event.
* **History:** 2024-01-01 onwards.
* **Refresh:** hourly.

# Source

Materialised from the [orders](/tables/orders.md) table, joined to
[customers](/tables/customers.md) for tier enrichment. Drives the
[monthly revenue](/metrics/monthly_revenue.md) metric.
