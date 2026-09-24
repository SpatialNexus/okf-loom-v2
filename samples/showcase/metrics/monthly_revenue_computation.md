---
type: Attested Computation
title: Demonstrative monthly revenue computation
description: Fictional revenue contract showing OKF 0.2 sources, verification, lifecycle, and computation metadata without execution.
resource: /metrics/monthly_revenue_computation.md
tags: [revenue, demo, computation]
generated:
  by: openai/gpt-6-astra
  at: "2026-09-09T00:21:51Z"
verified:
  by: process:demonstration-only-not-an-actual-check
  at: "2026-09-01T12:00:00Z"
status: draft
stale_after: "2026-09-23T00:00:00Z"
runtime: postgres
parameters:
  - name: month_start
    type: date
    required: true
executor:
  resource: https://example.com/demonstration-only/revenue-runner
  receipt: [run_id, executed_sql, result]
attester:
  resource: https://example.com/demonstration-only/revenue-attester
sources:
  - id: revenue-definition
    resource: /metrics/monthly_revenue.md
    title: Fictional monthly revenue definition
  - id: query-scope
    resource: all queries in fictional Northwind analytics project
    author: process:demonstration-only
    usage_count: 42
    last_modified: "2026-08-31T12:00:00Z"
    usage_window:
      from: "2026-08-01T00:00:00Z"
      to: "2026-09-01T00:00:00Z"
usage_window:
  from: "2026-07-01T00:00:00Z"
  to: "2026-08-01T00:00:00Z"
---

# Demonstration only

All business data, verifier identity, verification time, usage counts, and
executor/attester destinations are **fictional demonstration records**, not
actual verification or production evidence. The generated actor identifies the
tool authoring the example, not the fictional checker. No computation has run.
The displayed machine-confirmed tier demonstrates derivation from a bare
verification mapping; it is advisory, never authorization or proof of correctness.

# Computation

```sql
SELECT SUM(gross_amount_usd) - SUM(refund_amount_usd) AS net_revenue
FROM orders
WHERE placed_at >= :month_start
  AND placed_at < :month_start + INTERVAL '1 month'
  AND status IN ('placed', 'fulfilled', 'refunded');
```

This illustrative SQL follows the [monthly revenue definition](/metrics/monthly_revenue.md).[^revenue-definition]
Loom reads, validates, and displays the contract; it does not bind parameters,
invoke the executor, verify receipts, or run the attester.

# Source signals and lifecycle

The scope source is non-navigable text.[^query-scope] Its own usage window
overrides the shared window. Usage is a coarse liveness signal, not a score.
Stable ids keep footnotes attributed if sources are reordered. The document is
draft regardless of its demonstrative verification event; staleness begins on
or after the recorded expiry. Neither status nor expiry rewrites the contract.

[^revenue-definition]: Fictional Northwind monthly revenue definition.
[^query-scope]: Invented usage population for demonstrating source signals.
