---
okf_version: "0.1"
okf_extensions:
  - okf.cap.typed_relations
---

This bundle deliberately retains its OKF 0.1 declaration, legacy timestamps,
and citation forms for compatibility demonstrations. Fictional business content
is not verified evidence. Current 0.2 examples live in `samples/showcase` in the checkout.

# Coffee Shop Knowledge Bundle

A tiny demonstration bundle for the okf-loom. Covers the catalog of a
fictional coffee shop: datasets, tables, services, playbooks, and
references.

# Datasets

* [orders](datasets/orders.md) - the order-history dataset backing analytics.

# Tables

* [customers](tables/customers.md) - one row per registered customer.
* [orders](tables/orders.md) - one row per completed order.

# Services

* [checkout](services/checkout.md) - the order checkout API.

# Playbooks

* [refund_flow](playbooks/refund_flow.md) - how to triage a refund request.

# References

* [currencies](references/currencies.md) - ISO 4217 currency codes used in monetary columns.
