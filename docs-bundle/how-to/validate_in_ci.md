---
type: How-to
title: Validate a bundle in CI
description: Enforce OKF conformance in CI by picking a validation profile and wiring the strict validate command into GitHub Actions or GitLab CI.
resource: /how-to/validate_in_ci.md
tags: [ci, validate, github-actions, gitlab]
timestamp: "2026-06-29T00:00:00Z"
---

# Validate a bundle in CI

This guide turns validation into a CI gate that fails a pull request or
merge request the moment a concept breaks conformance or a recommended key
goes missing.

You already have a bundle on disk and a cloned okf-loom checkout with a working `okf` command or source-checkout invocation.
If you do not, start with [Use okf-loom from a clone](/tutorials/install.md) and
[Author your first bundle](/tutorials/first_bundle.md).

# Step 1: Pick a validation profile

The validator ships three profiles that remap finding severities for
different audiences without changing the wire format.

| Profile | When to use | What it does |
|---|---|---|
| `spec` (default) | Lightweight gate, spec conformance only. | Only `type` is required; everything else is a warning. |
| `producer` | Producer-side CI that wants rich metadata enforced. | `spec` plus `concept.missing_recommended_keys` promoted to ERROR. |
| `loose` | Ingesting incomplete docs that lack `type`. | `concept.missing_type` downgraded to WARNING. |

For a documentation bundle that you fully own, `producer` is usually the
right choice: it requires recommended titles, descriptions, resources, and tags.
It does not require generation/verification timestamps or certify provenance.
Present malformed optional metadata warns; `--strict` promotes those warnings.

For a bundle that ingests third-party markdown, `loose` lets the pipeline
land before you backfill `type`.

# Step 2: Decide on the broken-link policy

`--fail-on-broken-links` is orthogonal to the profile and promotes
`link.broken` findings to ERROR.

Use it when you want to block a release on dangling links, and leave it off
when you treat forward references as not-yet-written knowledge
(see [Link forms](/reference/links.md)).

# Step 3: Verify the exit codes locally

The validator exits `0` for conformant, `1` for conformance failure, and
`2` for strict failure.

```bash
scripts/okf-loom validate path/to/bundle --strict --profile producer --fail-on-broken-links
echo "exit code: $?"
```

The exit codes you will see:

| Code | Meaning |
|---|---|
| `0` | Conformant; no errors. |
| `1` | Conformance failure (a concept is missing `type`, or frontmatter is unparseable). |
| `2` | Strict failure (`--strict` was passed and warnings were promoted to errors). |

CI only needs to fail on non-zero, so any of `1` or `2` blocks the pipeline.

# Step 4: Wire it into GitHub Actions

Put the bundle path and the validate command in a workflow under
`.github/workflows/`.

```yaml
# .github/workflows/okf-validate.yml
name: OKF validate
on:
  pull_request:
    paths:
      - 'docs-bundle/**'
      - '.github/workflows/okf-validate.yml'
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Validate bundle
        run: |
          scripts/okf-loom validate docs-bundle \
            --strict \
            --profile producer \
            --fail-on-broken-links
```

The workflow fails the PR on any non-zero exit code; the `paths:` filter
only runs when bundle files actually change.

# Step 5: Wire it into GitLab CI

The same command drops into a `.gitlab-ci.yml` job.

```yaml
# .gitlab-ci.yml
okf:validate:
  image: python:3.11
  rules:
    - changes:
        - docs-bundle/**/*
        - .gitlab-ci.yml
  script:
    - scripts/okf-loom validate docs-bundle --strict --profile producer --fail-on-broken-links
```

GitLab treats any non-zero exit from `script:` as a job failure, so the
gate is identical to the GitHub Actions case.

# Step 6: Produce JSON for downstream tooling

Pass `--format json` to emit a machine-readable report that other CI steps
can consume.

```bash
scripts/okf-loom validate docs-bundle --profile producer --format json > okf-report.json
```

The JSON object carries the totals and the per-finding rows:

```json
{
  "ok": false,
  "errors": 1,
  "warnings": 3,
  "info": 0,
  "findings": [
    {
      "code": "concept.missing_recommended_keys",
      "severity": "error",
      "concept": "tables/orders",
      "message": "missing recommended key: description"
    }
  ]
}
```

A common pattern is to upload that file as a CI artifact and surface the
per-finding rows in a PR comment.

# Step 7: Lock the defaults in okf-loom.config.yaml

You can avoid repeating the profile and broken-link flag in every job by
pinning them in the bundle config.

```yaml
validate:
  default_profile: producer
  fail_on_broken_links: true
```

With those set,
`scripts/okf-loom validate docs-bundle --strict`
honours the bundle's defaults; CI scripts stay short.
See [okf-loom.config.yaml reference](/reference/config_yaml.md) for every key.

# End state

You now have a CI gate that:

- Runs `scripts/okf-loom validate` on every change to the bundle.
- Fails the pipeline on conformance errors, missing recommended keys
  (producer profile), and broken links.
- Emits a JSON report you can archive or comment onto a PR.

# See also

* [Find and fix bundle gaps](/how-to/discover_and_fix_gaps.md) — surface deeper curation issues beyond strict conformance.
* [Migrate across spec versions](/how-to/migrate_spec_version.md) — gate spec bumps behind the same validate.
* [CLI command reference](/reference/cli.md) — every flag on `validate`.
* [Frontmatter contract](/reference/frontmatter.md) — required vs recommended keys.
* [okf-loom.config.yaml reference](/reference/config_yaml.md) — pinning profile defaults.
* [What OKF is for](/explanation/what_is_okf.md) — why conformance is deliberately minimal.

Return to [How-to guides](/how-to/index.md).
