---
type: Dataset
title: Subscriptions (analytics)
description: Active-subscription dataset for MRR and churn analytics.
resource: https://warehouse.example.com/datasets/subscriptions
tags: [subscriptions, analytics, mrr]
timestamp: "2026-06-15T08:00:00Z"
provenance:
  source: billing service
  pipeline: dbt
---

# Coverage

* **Granularity:** one row per subscription per day.
* **History:** 2024-01-01 onwards.
* **Refresh:** daily.

# Source

Materialised from the [subscriptions](/tables/subscriptions.md) table.
Feeds the [monthly revenue](/metrics/monthly_revenue.md) metric.
