---
type: Reference
resource: /decisions/adopt_event_sourcing.md
title: ADR on Adopt Event Sourcing for Orders
description: Decision record; orders are persisted as an append-only event stream.
tags: [adr, architecture, orders]
timestamp: "2026-05-20T10:00:00Z"
status: stable
decision_status: accepted
deciders: [Alice (staff eng), Bob (platform lead)]
---

# Context

The [orders](/tables/orders.md) table is read by analytics, the
[checkout service](/services/checkout.md), and the
[refund flow](/playbooks/refund_flow.md). A mutable state column made
refunds and back-dated edits ambiguous.

# Decision

This fictional ADR's `decision_status: accepted` records its decision outcome;
OKF `status: stable` records the document lifecycle. The named deciders are
demonstrative, not `verified` events or evidence of actual human review.

Persist orders as an append-only event stream (`order.placed`,
`order.refunded`). Materialise the current-state table from the stream.

# Consequences

* **Pro:** full audit trail; trivial time-travel queries.
* **Con:** operational complexity; the
  [incident response](/playbooks/incident_response.md) runbook had to add
  event-replay steps.
* **Pro:** aligns with how [billing](/services/billing.md) already works.
