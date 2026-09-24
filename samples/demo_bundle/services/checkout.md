---
type: Service
title: Checkout Service
description: The order checkout API; accepts order POSTs and writes to the orders table.
resource: https://api.example.com/v2/orders
tags: [checkout, write-api, v2]
timestamp: "2026-06-20T14:00:00Z"
relations:
  - target: tables/orders
    type: writes_to
    detail: idempotent on client_request_id
  - target: tables/orders
    type: depends_on
    detail: checkout cannot function without the orders table
  - target: tables/customers
    type: depends_on
    detail: checkout validates the customer exists before accepting an order
---

# Overview

`checkout` is the public order-submission API. It validates cart state,
computes totals in the customer's [currency](/references/currencies.md),
and appends a row to the [orders table](/tables/orders.md).

# Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/v2/orders` | Create a new order. |
| GET | `/v2/orders/{id}` | Fetch an order by id. |
