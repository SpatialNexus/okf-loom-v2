---
type: BigQuery Table
title: Events
description: The events table, intentionally lacking a relative link elsewhere.
tags:
- events
timestamp: '2026-02-01T08:30:00Z'
---

# Events

One row per tracked event. The Users table is referenced via relative link:
[users](users.md).

# Schema

- `event_id` (INT64): primary key.
- `user_id` (INT64): FK to users.id.
