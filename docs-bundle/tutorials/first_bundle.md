---
type: Tutorial
title: Author your first bundle
description: Scaffold a new OKF bundle with scripts/okf-loom init, hand-write a few concepts, validate, discover gaps, and fix one.
resource: /tutorials/first_bundle.md
tags: [getting-started, authoring, init, discover]
timestamp: "2026-06-29T00:00:00Z"
---

# Author your first bundle

This tutorial takes you from an empty directory to a small, valid OKF
bundle that you scaffolded, populated, validated, and improved.

You will write the concepts by hand so you can see exactly what an OKF
file looks like.
Then you will let okf-loom check your work and suggest improvements.

This tutorial assumes you have finished
[Use okf-loom from a clone](/tutorials/install.md).
You need the checked-in source-checkout helper before you start.

# What you will have at the end

By the end of this tutorial you will have:

- A bundle created with `scripts/okf-loom init`.
- Three hand-written concepts linked together.
- A directory `index.md` listing your concepts.
- A clean `scripts/okf-loom validate` run.
- Experience reading a discovery report and fixing one gap.

You will be able to scaffold, populate, and validate a bundle on your own.

# Step 1: Scaffold with scripts/okf-loom init

okf-loom ships a scaffolding command that creates the three files
every well-formed bundle starts with.

Pick a name for your bundle and run:

```bash
scripts/okf-loom init --bundle ~/my-first-bundle --name "My First Bundle"
```

`scripts/okf-loom init` refuses to run in a non-empty directory, so you get a clean
slate every time.
If the directory exists and has files in it, the command exits with a
message — pick a fresh path and try again.

# Step 2: Inspect what init created

Move into the new bundle and list it:

```bash
cd ~/my-first-bundle
ls -la
```

You should see three files:

```text
index.md
log.md
okf-loom.config.yaml
```

Open `index.md`.
It is the bundle root index, and it is the only `index.md` allowed to
carry frontmatter.
The frontmatter declares the spec version and any optional extensions:

```yaml
---
okf_version: "0.2"
okf_extensions:
  - okf.cap.typed_relations
generated: true
---
```

The body is a directory listing — currently empty, because you have not
written any concepts yet.

Open `okf-loom.config.yaml`.
Every key is optional and commented; you can leave it at the defaults for
now.

Open `log.md`.
It is an append-only update history, newest first, that okf-loom will
help you maintain as the bundle grows.

# Step 3: Write your first concept

Create a directory for your first domain and add a concept.

```bash
mkdir tables
```

Create `tables/customers.md`:

```markdown
---
type: Table
title: Customers
description: One row per registered customer.
resource: /tables/customers.md
tags: [customers, pii]
---

# Schema

| Column | Type | Description |
|---|---|---|
| `customer_id` | STRING | Globally unique customer identifier. |
| `email` | STRING | Verified email address. |
| `first_seen_at` | TIMESTAMP | When the customer first appeared. |

# Notes

Email is considered PII.
```

A few things to notice:

- `type` is the only field the spec hard-requires; the rest are
  recommended but worth filling in.
- Generation and verification are optional and deliberately omitted in these
  hand-copyable templates. Record truthful `generated.by` and an aware
  `generated.at` when known; never infer a human reviewer or a historical
  timestamp from copying this tutorial. Default `write-concept` creation instead
  records the actual Loom tool actor and current UTC time.
- `tags` is a YAML list, not a comma-separated string.
- The body uses a `# Schema` table, which is the conventional shape for a
  table concept.

# Step 4: Add a second concept that links to the first

Create `tables/orders.md`:

```markdown
---
type: Table
title: Orders
description: One row per completed customer order.
resource: /tables/orders.md
tags: [orders, revenue]
---

# Schema

| Column | Type | Description |
|---|---|---|
| `order_id` | STRING | Globally unique order identifier. |
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
| `total_usd` | NUMERIC | Order total in US dollars. |
| `placed_at` | TIMESTAMP | When the order was submitted. |
```

The link `[customers](/tables/customers.md)` is in the absolute
bundle-relative form the spec recommends.
It is stable if you ever move `orders.md` around inside the bundle.

# Step 5: Add a third concept in a different domain

Concepts do not all have to live in one directory.
Add a service that writes to your orders table:

```bash
mkdir services
```

Create `services/checkout.md`:

```markdown
---
type: Service
title: Checkout Service
description: The order checkout API; accepts order POSTs and writes to the orders table.
resource: /services/checkout.md
tags: [checkout, write-api]
---

# Overview

`checkout` is the public order-submission API.
It validates cart state and appends a row to the [orders table](/tables/orders.md).

# Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/v2/orders` | Create a new order. |
| GET | `/v2/orders/{id}` | Fetch an order by id. |
```

You now have three concepts across two directories, linked into a small
graph.

# Step 6: Write a directory index

An `index.md` in any directory lets a reader (human or agent) see what is
available before opening individual files.

Create `tables/index.md` — and note that, unlike the bundle root, this
non-root index must **not** have frontmatter:

```markdown
# Tables

* [Customers](customers.md) - one row per registered customer.
* [Orders](orders.md) - one row per completed customer order.
```

You can also let okf-loom generate directory indexes for you with
`index`; hand-authoring one here shows you the shape directly.

# Step 7: Validate the bundle

Run the validator:

```bash
scripts/okf-loom validate ~/my-first-bundle
```

You should see a clean PASS:

```text
Conformance: PASS  Strict: PASS  (errors=0 warnings=0 info=0)
```

If you see warnings, read them — they are almost always missing
recommended keys or a typo in a link target, both easy to fix.

# Step 8: Discover gaps

Validation tells you whether the bundle is well-formed.
Discovery tells you what is missing or weakly connected.

Run the discovery pass and write the report to a file:

```bash
scripts/okf-loom discover ~/my-first-bundle --out ~/my-first-bundle-suggestions.json
```

Open the report.
With three concepts you will commonly see suggestions like these:

| Rule | What it flags |
|---|---|
| `missing_descriptions` | A concept whose `description` is empty. |
| `missing_indexes` | A directory with concepts but no `index.md`. |
| `orphan_concepts` | A concept with no incoming or outgoing links. |
| `unlinked_mentions` | A concept body that names another concept by title but does not link to it. |

Discovery suggestions are advisory.
The managing agent applies the ones that make sense and leaves the rest.

# Step 9: Fix one gap

Pick one concrete gap and fix it by hand.
A common one is a missing directory index for `services/`.

Create `services/index.md`:

```markdown
# Services

* [Checkout Service](checkout.md) - the order checkout API.
```

Re-run discovery to confirm the suggestion is gone:

```bash
scripts/okf-loom discover ~/my-first-bundle --out ~/my-first-bundle-suggestions.json
```

Then re-validate to confirm nothing regressed:

```bash
scripts/okf-loom validate ~/my-first-bundle
```

That loop — write, validate, discover, fix — is the daily authoring
rhythm for an OKF bundle.

# Where to go next

You can now scaffold, populate, and validate a bundle on your own.

When you want to speed up authoring, the CLI mutator verbs write concepts
and links for you so you do not have to hand-edit every file:

* [Author concepts via CLI verbs](/how-to/author_with_verbs.md) — `write-concept`, `link-add`, `entity-add`.
* [Find and fix bundle gaps](/how-to/discover_and_fix_gaps.md) — turning discovery output into applied edits.

The format details behind every field above are documented in:

* [Frontmatter contract](/reference/frontmatter.md) — required vs recommended keys.
* [Link forms](/reference/links.md) — absolute vs relative, and when to use each.

For the philosophy behind the format's tiny required surface, see
[What OKF is for](/explanation/what_is_okf.md).

Return to [Tutorials](/tutorials/index.md).
