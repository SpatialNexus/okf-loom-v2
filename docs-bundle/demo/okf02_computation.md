---
type: Attested Computation
title: OKF 0.2 metadata without execution
description: A demonstrative computation contract with truthful tool authorship, source attribution, optional trust, and lifecycle metadata.
resource: /demo/okf02_computation.md
tags: [demo, okf02, metadata, computation]
generated:
  by: openai/gpt-6-astra
  at: "2026-09-09T00:21:51Z"
status: draft
runtime: sql
parameters:
  - name: year
    type: integer
    required: true
executor:
  resource: /reference/frontmatter.md
  receipt: [run_id, executed_sql, result]
attester:
  resource: /reference/spec.md
sources:
  - id: okf02
    resource: https://raw.githubusercontent.com/GoogleCloudPlatform/open-knowledge-format/main/SPEC.md
    title: Upstream Open Knowledge Format, Version 0.2
---

# OKF 0.2 metadata without execution

This concept demonstrates a contract, not a runnable service or attested result.
The executor and attester resources deliberately point to explanatory docs, not
code. No receipt has been produced or checked. `generated` identifies the tool
that authored this example; no human verification is claimed.

The format records computation metadata without prescribing an execution
runtime protocol.[^okf02] Loom reads, validates, and displays these fields only.

# Computation

```sql
SELECT SUM(amount) AS revenue
FROM example_recognized_revenue
WHERE fiscal_year = :year;
```

The table and binding syntax are illustrative. Nothing in this checkout runs
this SQL or the resources named in frontmatter.

# Trust and lifecycle

There is no `verified` key: this page is **unverified**, not machine-confirmed
merely because a tool authored it. `status: draft` is separate from verification.
No expiry is recorded, so no freshness claim is made.

The following **fictional records illustrate syntax only**; they are not this
page's provenance and do not assert actual review:

```yaml
verified: {by: "human:demonstration-only", at: "2026-09-01T12:00:00Z"}
stale_after: "2026-09-23T00:00:00Z"
sources:
  - id: illustrative-query-scope
    resource: all queries in fictional project Northwind
    author: process:demonstration-only
    usage_count: 42
    last_modified: "2026-08-31T12:00:00Z"
    usage_window: {from: "2026-08-01T00:00:00Z", to: "2026-09-01T00:00:00Z"}
usage_window: {from: "2026-07-01T00:00:00Z", to: "2026-08-01T00:00:00Z"}
```

A bare verification mapping is one event; a list permits independent events.
Trust tiers are advisory, never permission to act. Staleness includes equality
at the expiry instant. A source's window overrides the shared window; counts
are coarse usage signals, not credibility scores.

# Attribution and compatibility

The `okf02` footnote joins the exact source id, even if the source list is
reordered. It is independent of Loom `citations` and exact Context Pack
`[path.md#Lx-Ly]` citations. Existing authored timestamps are retained elsewhere
in this bundle; fallback uses them only when `generated` is absent.

Read the [frontmatter reference](/reference/frontmatter.md),
[migration guide](/how-to/migrate_spec_version.md), and
[rendering showcase](/demo/showcase.md) for the surrounding contracts.

[^okf02]: Upstream OKF Version 0.2, sections 5 and 10, fetched 2026-09-09.
