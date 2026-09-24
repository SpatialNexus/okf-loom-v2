---
type: Dataset
title: Orders Dataset
description: The order-history dataset backing analytics and the customer-facing order history page.
resource: https://warehouse.example.com/datasets/orders
tags: [analytics, orders]
timestamp: "2026-06-27T10:00:00Z"
---

# Overview

The `orders` dataset groups every order-related table in the warehouse. It
is populated by the [checkout service](/services/checkout.md) and surfaced
through the [orders table](/tables/orders.md).

# Refresh cadence

Every 15 minutes via the `orders-pipeline` Airflow DAG.
