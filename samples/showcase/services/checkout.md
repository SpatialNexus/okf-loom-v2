---
type: Service
title: Checkout API
description: REST API that creates orders and charges customers via Stripe or PayPal.
resource: https://github.com/northwind/checkout
tags: [orders, api]
timestamp: "2026-06-15T08:00:00Z"
entities:
  - label: Stripe
    kind: vendor
    source: https://stripe.com
  - label: PayPal
    kind: vendor
    source: https://www.paypal.com
  - label: Kafka
    kind: system
    source: https://kafka.apache.org
---

# Responsibilities

* Accept order payloads from web and mobile clients.
* Authorise the charge via **Stripe or PayPal** (the provider is selected per
  order, by market, currency, and customer preference; both are first-class
  integrations).
* Emit an `order.placed` event to Kafka.
* Persist the order to the [orders](/tables/orders.md) table.

# Dependencies

Depends on the [customers](/tables/customers.md) table for identity and on
**Stripe and PayPal** for payment authorisation. Either provider can authorise
a given charge; failure of payment authorisation triggers the
[refund flow](/playbooks/refund_flow.md).

# SLO

p99 latency < 800 ms. Pager escalation per
[incident response](/playbooks/incident_response.md).
