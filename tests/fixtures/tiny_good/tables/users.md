---
type: BigQuery Table
title: Users
description: The users table.
resource: https://example.com/users
tags:
- users
- identity
timestamp: '2026-01-15T10:00:00Z'
relations:
- target: references/metrics
  type: references
  detail: user-count metric
---

# Users

The `users` table stores one row per registered user.

See [Metrics](/references/metrics.md) for the user-count metric that joins
against this table. The Events table is also related: [events](events.md).

# Schema

- `id` (INT64): primary key.
- `name` (STRING): display name.
- `created_at` (TIMESTAMP): registration time. FK to references/metrics via user_id.

## Citations

- https://example.com/citations/users
