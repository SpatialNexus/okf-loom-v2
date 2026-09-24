---
type: Reference
resource: /references/sla_tiers.md
title: Customer SLA Tiers
description: Definition of each customer tier and its response-time SLA.
tags: [reference, sla, customers]
timestamp: "2026-06-15T08:00:00Z"
---

# Tiers

| Tier | Response SLA | Description |
|---|---|---|
| `starter` | 8 business hours | Self-serve only. |
| `growth` | 4 business hours | Email support. |
| `enterprise` | 1 hour, 24/7 | Pager + dedicated CSM. |

# Notes

The `tier` column on [customers](/tables/customers.md) drives routing in
[incident response](/playbooks/incident_response.md).
