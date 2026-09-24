---
type: Playbook
resource: /playbooks/refund_flow.md
title: Refund Flow
description: How to triage and process a customer refund request.
tags: [refunds, support, finance]
timestamp: "2026-06-15T08:00:00Z"
---

# When to use

A customer requests a refund for an [order](/tables/orders.md). Use this
playbook for any refund up to $500 USD; above that, escalate to finance.

# Steps

1. **Confirm the order.** Look up the `order_id` in the
   [orders](/tables/orders.md) table. Confirm it is in `placed` or
   `fulfilled` status.
2. **Check the customer.** Open [customers](/tables/customers.md) and
   confirm the `customer_id` and SLA tier.
3. **Issue the refund.** Use the Stripe dashboard (see
   [checkout](/services/checkout.md) for the Stripe account reference).
4. **Record it.** Set the order `status` to `refunded` and append to
   `log.md` per SPEC §7.
5. **Notify.** Email the customer using the address on file.

# Edge cases

* **Cross-currency refunds:** refund in the original
  [currency](/references/currencies.md).
* **Subscription refunds:** coordinate with the
  [billing service](/services/billing.md).

# Talking to finance

Refunds **over $500 USD** (or any refund a support agent is unsure about)
must be escalated to finance before processing. Use the `#finance-refunds`
Slack channel (or `finance-refunds@northwind.example.com` for async) and
include every field below so finance can approve without a back-and-forth.

**Sample escalation message:**

```
@finance-refunds: refund approval needed

order_id:        ord_8f3a91c2
customer_id:     cus_4b7e (tier: Enterprise; see customers)
amount:          USD 742.00
original_charge: Stripe charge ch_3OqW... (PayPal: PAYID-MX7...)
reason:          Duplicate charge; customer was double-billed during the
                 2026-06-27 checkout outage.
requested_by:    support agent (you)
context:         orders.gross_amount_usd already adjusted; refund would bring
                 the order to status=refunded.
```

**What finance needs from you (checklist):**

* `order_id` + the [orders](/tables/orders.md) row (status before refund).
* `customer_id` + SLA tier from [customers](/tables/customers.md).
* Refund amount + original [currency](/references/currencies.md).
* The original charge reference (Stripe `ch_…` or PayPal `PAYID-…`; see
  [checkout](/services/checkout.md)).
* The reason, in one or two sentences.
* Whether a [subscription](/tables/subscriptions.md) / the
  [billing service](/services/billing.md) is involved.

Finance responds in the same channel; do **not** process the refund until
you have explicit approval. Once approved, resume at step 3 above and note
"approved by finance" in the `log.md` entry.
