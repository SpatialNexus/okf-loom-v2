---
type: Service
title: Billing Service
description: Manages subscriptions, renewals, dunning, and invoicing.
resource: https://github.com/northwind/billing
tags: [subscriptions, billing, api]
timestamp: "2026-06-15T08:00:00Z"
entities:
  - label: Stripe
    kind: vendor
    source: https://stripe.com
---

# Responsibilities

* Create, renew, and cancel subscriptions.
* Retry failed renewals (dunning) via Stripe.
* Emit `subscription.renewed` events.
* Mirror state to the [subscriptions](/tables/subscriptions.md) table.

# Dependencies

Reads [customers](/tables/customers.md) for tier and contact info.
Drives the [subscriptions analytics](/datasets/subscriptions.md) dataset.
Outages follow [incident response](/playbooks/incident_response.md).
