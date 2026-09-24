---
type: Playbook
resource: /playbooks/incident_response.md
title: Incident Response
description: On-call runbook for production outages and degraded services.
tags: [oncall, sre, incidents]
timestamp: "2026-06-15T08:00:00Z"
---

# Severity

Match the customer's SLA tier from [SLA tiers](/references/sla_tiers.md)
to decide response time.

# Steps

1. **Acknowledge** the page within the SLA window.
2. **Open the bridge.** Use the on-call Zoom link.
3. **Identify the blast radius.** Which
   [services](/services/checkout.md) and
   [tables](/tables/orders.md) are affected?
4. **Mitigate.** Roll back, scale out, or shed load.
5. **Resolve.** Confirm green on the dashboard.
6. **Postmortem.** File within 48h; link the ADR if architectural
   (e.g. [event sourcing](/decisions/adopt_event_sourcing.md)).

# Common failures

* **Checkout 5xx:** usually Stripe timeouts; see
  [checkout](/services/checkout.md).
* **Billing lag:** see [billing](/services/billing.md).
