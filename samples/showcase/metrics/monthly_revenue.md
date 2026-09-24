---
type: Metric
title: Monthly Revenue
description: Gross revenue per calendar month, net of refunds.
resource: https://metrics.example.com/monthly_revenue
tags: [revenue, metric, finance]
timestamp: "2026-06-15T08:00:00Z"
provenance:
  source: warehouse
  pipeline: dbt
---

# Definition

The [OKF 0.2 computation example](/metrics/monthly_revenue_computation.md) shows
how to describe this fictional metric with sources, trust, and lifecycle.
Neither that contract nor the illustrative query below is executed by Loom.

```sql
SELECT
  DATE_TRUNC('month', placed_at) AS month,
  SUM(gross_amount_usd) - SUM(refund_amount_usd) AS net_revenue
FROM orders
WHERE status IN ('placed', 'fulfilled', 'refunded')
GROUP BY 1
```

# Inputs

* [orders](/tables/orders.md): gross amounts.
* [customers](/tables/customers.md): tier enrichment.
* [subscriptions](/datasets/subscriptions.md): MRR component.

# Caveats

Refund timing follows the [refund flow](/playbooks/refund_flow.md); a
refund in month N can reduce month N-0 revenue if back-dated.
