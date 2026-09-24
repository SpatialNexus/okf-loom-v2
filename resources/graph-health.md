# Graph health

Graph health is advisory. A bundle can be valid OKF and still be hard to use
as a graph; do not turn these checks into hard conformance unless the user asks
for a producer policy.

## Quick loop

```bash
scripts/okf-loom validate <bundle>
scripts/okf-loom discover <bundle>
scripts/okf-loom graph-quality <bundle>
scripts/okf-loom serve <bundle> --no-open
```

Use `--format json` when another agent or harness will consume the report.

## What good looks like

- `type` names the kind of thing: `Table`, `Service`, `Runbook`, `Decision`,
  `Redmine Issue`, `Historical Wiki Revision`. Do not use it for broad source
  buckets such as `Wiki Page` when a more precise type is available.
- `title` names the instance. Duplicate titles should either be merged or made
  unambiguous.
- Links and `relations:` express real dependency, membership, reference, or
  provenance edges. For hierarchy, prefer relation types such as `part_of`,
  `contains`, or `belongs_to` when the relationship is semantically true.
- Imported or mixed-source bundles should carry source metadata such as
  `source_system` and, when helpful, `graph_cluster`. These are optional custom
  keys; consumers that do not understand them must still ignore them safely.
- Add `aliases`, `entities`, and `provenance` when they carry real information.
  Do not invent filler values just to satisfy a report.
- Prefer absolute bundle-relative Markdown links in generated or newly authored
  prose, for example `[Orders](/tables/orders.md)`. Relative links remain valid.

## Reading `graph-quality`

`graph-quality` reports common usability smells:

- generic type overuse;
- duplicate or near-duplicate titles;
- low optional metadata coverage;
- no explicit hierarchy signal in larger bundles;
- disconnected components and orphan concepts.

Treat findings as prompts for review. A deliberately separated bundle may have
multiple components; a small scratch bundle may have no aliases or provenance.

## Discovery noise

`discover` suppresses low-confidence unlinked-mention suggestions by default.
Use:

```bash
scripts/okf-loom discover <bundle> --include-low-confidence
```

when doing a broad import audit. Do not blindly apply mention suggestions whose
matched phrase is a common label such as `Users`, `Clients`, `Wiki`, or `Index`,
a common first name such as `David`, or a project/status label that appears
across many imported pages. High-frequency labels usually need source-aware
curation, not automatic linking.

If JSON output contains suppressed suggestions, inspect
`detail.suppression_reasons` before deciding whether to override the default.
`already_structurally_related` means the source already has a `relations:`
edge to the target, so adding a body link is usually redundant. Generic labels
such as `Architecture`, `README`, or `Implementation Plan` are intentionally
low confidence unless source and target share context through `graph_cluster`
or folder placement.

When a broad alias is useful for search but bad for automatic linking, keep it
as an object-form alias with `discoverable: false`, for example
`aliases: [{label: Architecture, discoverable: false}]`. Search and the
viewer still use the label; `discover` does not.

For unlinked mentions, inspect `detail.location_counts`. Body prose is the
strongest signal. Matches that appear only in `frontmatter`, `h1`, `heading`,
or `table` locations are intentionally low confidence because they often echo
section titles, page titles, or imported table labels rather than expressing a
useful outgoing link.

Use bundle config for editorial suppressions that are local to one bundle:

```yaml
discover:
  suppress_phrases: [architecture, product spec]
  suppress_pairs:
    - source: /project-a/product-spec.md
      target: /project-b/product-spec.md
```

Configured suppressions stay visible in JSON as `configured_phrase` or
`configured_pair` reasons.

Use `actionability_counts` and the `actionability` buckets to triage large
reports. Start with `safe_to_apply`, review `needs_review`, and treat
`suppressed_existing_relation`, `suppressed_generic_label`,
`suppressed_cross_cluster`, `suppressed_configured`, and `low_confidence` as
explanatory buckets unless the user explicitly asks for a broad audit.
