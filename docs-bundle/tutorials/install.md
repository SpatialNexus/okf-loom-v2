---
type: Tutorial
title: Use okf-loom from a clone
description: Clone the okf-loom skill repository, load SKILL.md and focused resources, verify source-checkout invocation, and run your first validation against a tiny scratch bundle.
resource: /tutorials/install.md
tags: [clone, getting-started, validate, skills]
timestamp: "2026-06-29T00:00:00Z"
---

# Use okf-loom from a clone

This tutorial takes you from an empty machine to a working source checkout
of okf-loom, and then walks you through your very first validation
against a bundle you build by hand.

The primary distribution model is this GitHub-hosted, cloneable skill
repository. Agents and harnesses should clone the repo, read `SKILL.md`
(or `AGENTS.md` if their harness starts there), load the focused resource
files under `resources/`, and run okf-loom from the checkout.

You do not need to know anything about OKF to follow along.
Every step is runnable; copy the commands in order.

# What you will have at the end

By the end of this tutorial you will have:

- A cloned okf-loom repository.
- `SKILL.md` and the relevant focused resource loaded or identified for your harness.
- A verified source-checkout command that prints okf-loom version.
- A tiny two-concept bundle that passes `scripts/okf-loom validate`.
- A clear mental model of what "conformance" means for an OKF bundle.

# Step 1: Check your Python

okf-loom requires Python 3.11 or newer.
Check what you have:

```bash
python3 --version
```

You should see `Python 3.11.x` or higher.
If you see an older version, install Python 3.11+ first
([python.org](https://www.python.org/downloads/) or your system package
manager) before continuing.

# Step 2: Clone the repository

Use the supplied fork checkout (supported local checkout: `ui-analysis`). If obtaining another
checkout, use a maintainer-provided repository location; no fork publication URL
is declared here. The repository itself is the git-loadable skill, not a package:

```bash
git clone <repo-url> okf-loom
cd okf-loom
```

If you already have the repository, start from its root directory instead.

# Step 3: Load the agent-facing skill resources

If you are an agent, read `SKILL.md` first, then load the smallest
resource file that fits your task:

| Resource file | Use |
|---|---|
| `resources/format-basics.md` | Read, validate, or make a small edit. |
| `resources/authoring.md` | Author or restructure concepts. |
| `resources/advanced-operations.md` | Use search, discovery, update plans, embedding, or the live studio. |
| `resources/studio-agent-loop.md` | Drive comment-directed live studio work. |

Humans can skim `README.md` and continue.

# Step 4: Create a virtual environment

It is good practice to install Python tools into a dedicated virtual
environment so they do not collide with your system Python.

```bash
python3 -m venv .venv
```

Activate it:

```bash
# Linux and macOS:
source .venv/bin/activate

# Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

Your shell prompt should now show `(.venv)`.
Every Python command from here on runs inside that environment.

# Step 5: Run directly from the checkout

For the most portable agent path, run directly from the checkout:

```bash
scripts/okf-loom --version
```

Normal runtime and docs validation require no primary dependency install step.
PyYAML is preferred when present; when it is not available, okf-loom uses a conservative built-in YAML fallback that fails closed on unsupported advanced YAML.
For local test development, install test dependencies explicitly:

```bash
python -m pip install pytest
```
This repo is not installed as a package for normal use; `scripts/okf-loom` is the source-checkout contract.

## Optional: the browser-proof dependencies

okf-loom ships an optional browser-based end-to-end proof for the
viewer (Playwright tests + a screenshot-capture script).
It is **not** required to use okf-loom, the viewer, or the live studio.

Install it only if you want to run the viewer e2e proof:

```bash
python -m pip install playwright
playwright install chromium
```

Without these dependencies, the browser proof tests skip cleanly and everything
else works exactly the same.
Most readers should skip this for now and come back to it later.

# Step 6: Remember the helper path

The checked-in helper can be run from any current directory.
Save the checkout path before leaving the repo:

```bash
export OKF_REPO="$(pwd)"
```

Now verify it:

```bash
"$OKF_REPO/scripts/okf-loom" --version
```

You should see something like:

```text
okf-loom 2.0.0 (SPEC v0.2)
```

The first number is okf-loom version; the second is the OKF spec
version okf-loom implements.
Both come from okf-loom itself — you do not need to set them.

# Step 7: Build a tiny scratch bundle

An OKF bundle is just a directory of markdown files with YAML
frontmatter.
You do not need the scaffolding command for this first check — two
hand-written files are enough to prove the install works.

Make a scratch directory:

```bash
mkdir -p ~/okf-scratch/tables
cd ~/okf-scratch
```

Write your first concept.
Create `tables/customers.md` with this content:

```markdown
---
type: Table
title: Customers
description: One row per registered customer.
tags: [customers]
---

# Schema

| Column | Type | Description |
|---|---|---|
| `customer_id` | STRING | Globally unique customer identifier. |
| `email` | STRING | Verified email address. |
```

Write a second concept that links to the first.
Create `tables/orders.md`:

```markdown
---
type: Table
title: Orders
description: One row per completed customer order.
tags: [orders]
---

# Schema

| Column | Type | Description |
|---|---|---|
| `order_id` | STRING | Globally unique order identifier. |
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
```

You now have a bundle with two concepts and one link between them.

# Step 8: Validate the scratch bundle

Run the validator against the directory:

```bash
"$OKF_REPO/scripts/okf-loom" validate ~/okf-scratch
```

You should see output ending in a clean PASS:

```text
Conformance: PASS  Strict: PASS  (errors=0 warnings=0 info=0)
```

The `Conformance: PASS` line is the one that matters.
It means your bundle meets every hard rule in the OKF spec.

# What conformance means

The OKF spec is deliberately tiny.
A bundle is conformant when **every** non-reserved markdown file has
parseable YAML frontmatter with a non-empty `type` field.
That single `type` key is the only hard-required field in the whole
format.

Everything else — `title`, `description`, `tags`, optional metadata, links —
is **recommended**, not required.
The validator surfaces missing recommended keys as warnings, not errors,
so a bundle with a few warnings is still perfectly conformant.

This shows up in the exit codes:

| Code | Meaning |
|---|---|
| `0` | Conformant (no errors). |
| `1` | Conformance failure (a concept is missing `type`, or frontmatter is unparseable). |
| `2` | Strict failure (you passed `--strict` and warnings were promoted to errors). |

For a quick CI gate you will usually want `scripts/okf-loom validate --strict` from inside `okf-loom/`, or `okf-loom/scripts/okf-loom validate --strict` from a parent workspace, which
treats warnings as errors.
That is a choice you make at validate time — the bundle on disk stays the
same.

# Where to go next

You now have a working source checkout and have validated a bundle by hand.

The natural next step is to let okf-loom scaffold a bundle for you and
walk through the full authoring loop:

* [Author your first bundle](/tutorials/first_bundle.md) — use `scripts/okf-loom init`, write concepts, validate, and discover gaps.

When you are ready to turn validation into a CI gate:

* [Validate a bundle in CI](/how-to/validate_in_ci.md) — strict profiles and broken-link gates.
* [Build a static site](/how-to/build_static_site.md) — render a bundle to a portable site.

The machinery behind every command above is documented neutrally in:

* [CLI command reference](/reference/cli.md) — every command and flag.
* [Frontmatter contract](/reference/frontmatter.md) — required vs recommended keys.

For the bigger picture of what OKF is for, see
[What OKF is for](/explanation/what_is_okf.md).

Return to [Tutorials](/tutorials/index.md).
