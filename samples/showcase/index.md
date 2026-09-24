---
okf_version: "0.2"
okf_extensions:
  - okf.cap.typed_relations
  - okf.cap.entities
  - okf.cap.aliases
  - okf.cap.citations
  - okf.cap.provenance
---

# Northwind Coffee Knowledge Base

A demonstration OKF bundle for a fictional specialty-coffee company.
Covers **datasets, tables, services, playbooks, references, metrics,
glossary, and decisions**, and is richly cross-linked so the graph,
search, and discovery features all have something to show.

This is an OKF 0.2 example with intentionally retained legacy timestamps and
Loom extensions. Names, verification events, source usage, and business claims
are **demonstrative**, not actual human review or production evidence.
For the retained 0.1 compatibility bundle, see `samples/demo_bundle` in the checkout.

# Datasets

* [orders](datasets/orders.md): order-history analytics dataset.
* [subscriptions](datasets/subscriptions.md): recurring subscription dataset.

# Tables

* [customers](tables/customers.md): one row per registered customer (PII).
* [orders](tables/orders.md): one row per completed order.
* [subscriptions](tables/subscriptions.md): one row per active subscription.

# Services

* [checkout](services/checkout.md): order checkout API.
* [billing](services/billing.md): subscription billing service.

# Playbooks

* [refund_flow](playbooks/refund_flow.md): how to triage a refund request.
* [incident_response](playbooks/incident_response.md): on-call runbook for outages.

# References

* [currencies](references/currencies.md): ISO 4217 currency reference.
* [sla_tiers](references/sla_tiers.md): customer SLA tier definitions.

# Metrics

* [monthly_revenue](metrics/monthly_revenue.md): gross revenue per month.
* [monthly_revenue_computation](metrics/monthly_revenue_computation.md): demonstrative metadata-only computation with sources, trust, and lifecycle.

# Glossary

* [glossary](glossary/index.md): domain terms.

# Decisions

* [adopt_event_sourcing](decisions/adopt_event_sourcing.md): ADR on event-sourced orders.
