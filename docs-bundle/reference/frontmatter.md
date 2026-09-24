---
type: Reference
title: Frontmatter contract
description: The OKF frontmatter contract (SPEC §4.1) — required and recommended keys, round-trip preservation, and YAML gotchas.
resource: /reference/frontmatter.md
tags: [frontmatter, yaml, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# Frontmatter contract

Every OKF concept is a markdown file whose body is preceded by a YAML
frontmatter block delimited by `---` lines. The contract is defined by
SPEC §4.1 and enforced by [`scripts/okf-loom validate`](cli.md#validate).
See [index.md](/reference/index.md) for the rest of the reference quadrant.

A minimal conformant concept:

```markdown
---
type: Reference
---

# Body

Free markdown.
```

That is the entire hard requirement: parseable YAML and a non-empty
`type`. Everything else in this document is recommendation or convention.

# Keys

## Required

| Key | Type | Notes |
|---|---|---|
| `type` | string | **Always required by upstream OKF 0.2 §11.** Short string naming the kind of thing (e.g. `Table`, `API Endpoint`, `Tutorial`, `Reference`). Not centrally registered; consumers tolerate unknown types. |

## Recommended

| Key | Type | Default | Notes |
|---|---|---|---|
| `title` | string | filename stem (underscores → spaces) | Human display name. Index generators and the viewer use it. |
| `description` | string | empty | One-line summary. Used by index generators and search snippets. |
| `resource` | string (URI) | absent | Canonical URI of the underlying asset. Absent for abstract concepts. |
| `tags` | list of strings | empty | Cross-cutting themes. Must be a YAML list. |

Missing recommended keys surface as `WARNING` findings under the
default `spec` profile; they become `ERROR` under the `producer`
profile (see [cli.md § validate](cli.md#validate)).

## Optional OKF 0.2 families

These are upstream fields, not opt-in Loom capabilities. Missing families do not
produce conformance errors. Present malformed values receive soft warnings;
`--strict` promotes warnings, while `producer` promotes missing recommended keys.

| Field | Meaning and reader behavior |
|---|---|
| `generated` | Mapping with required-within-family `by`; optional `at` records last meaningful content change. |
| `timestamp` | Legacy content-change fallback **only when `generated` is absent**. Never required or automatically rewritten. |
| `verified` | List of `{by, at}` events; a bare mapping is a singleton list. Invalid events do not establish a trust tier. |
| `status` | `draft`, `stable`, `deprecated`; absent means `stable`. |
| `stale_after` | Stale on/after this absolute instant, including equality. Missing/invalid expiry is unknown, not freshness proof. |
| `sources` | List of sources, each with required-within-entry `resource`; optional stable `id`, `title`, `author`, `usage_count`, `last_modified`, `usage_window`. |
| `usage_window` | Shared `{from, to}` range; a source's own window overrides by presence. |
| Computation fields | `runtime`, `parameters`, `computation`, `executor`, `attester`; read/validate/display only. |

Every timestamp-valued field uses a real ISO 8601 datetime with an explicit
offset (`Z` or e.g. `+01:00`), not a date-only string, impossible date, or naive
datetime. Quote strings to preserve spelling. `generated.at` without a valid
`by`, or malformed present `generated`, does not permit legacy fallback.

Use actual tool actors (`<producer>/<version>`), processes (`process:<id>`), or
people (`human:<id>`). Never fabricate human authorship or verification. A valid
human verification event derives `human-reviewed`; other valid events derive
`machine-confirmed`; otherwise the tier is `unverified`. This is advisory
metadata, **never authorization or an execution verdict**.

A source `resource` can be a URL/path or a population/scope descriptor, such as
`all queries in project example`. Descriptors display as text, not guessed links.
Body `[^source-id]` footnotes join exact, unique `sources[].id` values; ids must
remain stable through reordering. `usage_count` is a nonnegative integer liveness
signal, not a score or precise cross-kind ranking. `last_modified` describes the
source, whereas `generated.at` describes the concept. Windows require aware
`from <= to`; an invalid entry override does not silently inherit the shared one.

For `type: Attested Computation`, upstream calls for `runtime`; Loom warns if
absent. Parameters are `{name, type, required}` mappings (`required` is boolean).
Use `computation` for a path or a single body fence under `# Computation`.
`executor` has `resource` and optional `receipt` field-name list; `attester` has
`resource`. Loom never dereferences or executes those resources, verifies a
receipt, or defines the deferred execution/attestation protocol.

See the [canonical metadata semantics](/reference/spec.md#okf-02-metadata-semantics)
and [demonstrative computation](/demo/okf02_computation.md).

## Loom extension keys and round-trip

Producers MAY add arbitrary keys. Consumers — including okf-loom —
MUST preserve them verbatim on round-trip. okf-loom preserves
frontmatter key order and unknown keys through every
parse → modify → serialize cycle.

Several custom keys are recognised by the
[capability registry](capabilities.md) and activate capabilities by
their presence (auto-activation):

| Custom key | Activates |
|---|---|
| `relations` | `okf.cap.typed_relations` |
| `entities` | `okf.cap.entities` |
| `aliases` | `okf.cap.aliases` |
| `provenance` | `okf.cap.provenance` |
| `citations` | `okf.cap.citations` |

## `aliases`

Use `aliases:` for alternate names that should be searchable and visible in
the viewer. The legacy shorthand is a list of strings:

```yaml
aliases:
  - Purchase Orders
  - Sales Orders
```

When an alias is useful for search but too broad for automatic unlinked-mention
discovery, use object form and set `discoverable: false`:

```yaml
aliases:
  - label: Architecture
    discoverable: false
```

The alias remains indexed by search and shown by the viewer, but
`scripts/okf-loom discover` will not suggest links from incidental mentions of
that alias. String aliases are discoverable by default.

Example combining recommended + custom keys:

The actors in this snippet are illustrative, not actual provenance.

```yaml
---
type: API Endpoint
title: Create Order
description: POST a new order to the checkout service.
resource: https://api.example.com/v2/orders
tags: [checkout, write-api, v2]
generated: {by: "example-agent/1", at: "2026-06-15T10:00:00Z"}
relations:
  - target: services/checkout
    type: implemented_by
    detail: POST handler in checkout service
entities:
  - id: entity/order
    label: Order
    kind: business_entity
    aliases: [purchase, transaction]
citations:
  - id: "1"
    text: "Internal data dictionary"
    url: https://wiki.example.com/data/orders
owner: team-checkout
sla_tier: "1"
---
```

# Reserved filename frontmatter

| File | Frontmatter allowed? |
|---|---|
| Bundle-root `index.md` | Yes — `okf_version` (upstream §8/§12), plus Loom `okf_extensions` and boolean `generated` ownership marker. |
| Non-root `index.md` | **No.** Pure directory listing. |
| `log.md` | **No.** Upstream §9 structure only. |
| Any other `.md` | Yes — the full frontmatter contract applies. |

A bundle-root `index.md` declaring capabilities:

```yaml
---
okf_version: "0.2"
okf_extensions:
  - okf.cap.typed_relations
  - okf.cap.entities
generated: true
---
```

See [links.md](links.md) for the link contract and
[capabilities.md](capabilities.md) for the opt-in mechanisms.

Only literal root `generated: true` owns unmarked whole-body regeneration.
A provenance mapping or string `"true"` does not. Paired index markers own only
their enclosed body region. Concept generation metadata never grants write authority.

Loom `provenance` and `citations` remain supported extensions, separate from
OKF `sources` and stable-id footnotes. Legacy body `# Citations` remains readable;
neither form is automatically converted. Context Pack `[path.md#Lx-Ly]` citations
refer to exact physical source lines, not to any of these provenance fields.

# YAML gotchas

YAML 1.1 (the dialect PyYAML implements by default) silently coerces
unquoted scalars. okf-loom's validator downgrades
`timestamp_validity` findings to WARNING, but the underlying parse is
still lossy. Quote the following:

## Timestamps

YAML parses unquoted ISO dates as `datetime.datetime`, not as a string.

```yaml
# BAD — parses as datetime, not a string; round-trip may reformat it.
timestamp: 2026-06-29T00:00:00Z

# GOOD — preserved as a literal string.
timestamp: "2026-06-29T00:00:00Z"
```

This applies to any key carrying a datetime: `generated.at`, `verified[].at`,
`stale_after`, `sources[].last_modified`, usage-window endpoints, legacy `timestamp`,
`updated_at`, `created_at`, `provenance.collected_at`, etc.

## Version-like strings

YAML parses `1.10` as float `1.1` and `3.0` as float `3.0`. Quote
anything that must round-trip verbatim.

```yaml
# BAD — `1.10` becomes `1.1`; `3.0` becomes `3.0` (lossy on round-trip).
version: 1.10
bigquery_dataset_version: 3.0

# GOOD
version: "1.10"
bigquery_dataset_version: "3.0"
```

The same applies to numeric ids (`id: "0007"`) and SLA tiers
(`sla_tier: "1"`).

## Booleans

YAML 1.1 treats `yes`, `no`, `on`, `off`, `true`, `false` (case-insensitive)
as booleans. A tag literally named `no` becomes `False`. Quote any
string that coincides with a boolean token if you mean it as text.

```yaml
# BAD — `no` becomes boolean False.
tags: [yes, no, maybe]

# GOOD
tags: ["yes", "no", "maybe"]
```

Note: `okf-loom.config.yaml` accepts the boolean tokens for genuine boolean
fields (see [config_yaml.md](config_yaml.md) § Boolean coercion), but
the same rule does not apply to free-form frontmatter strings.

## `tags` must be a list

SPEC §4.1 implies a list. okf-loom tolerates a scalar tag, but the
canonical form is a YAML list.

```yaml
# BAD — a comma-separated string. Parses as one tag named "a, b, c".
tags: a, b, c

# GOOD — flow form.
tags: [a, b, c]

# GOOD — block form.
tags:
  - a
  - b
  - c
```

## Trailing whitespace and newlines

okf-loom normalises trailing whitespace to a single trailing
newline on write. Do not expect byte-identical round-trip if you
hand-format YAML; semantic identity is preserved.

## Aliases and merge keys

okf-loom's YAML loader (`_NodeLimitedSafeLoader`) disables
alias/anchor construction and merge keys as a DoS guard. Frontmatter
must be fully materialised; you cannot `<<: *base` to share blocks.

# Round-trip guarantees

okf-loom guarantees the following through every
parse → modify → serialize cycle (the `roundtrip.py` module):

| Property | Guarantee |
|---|---|
| Unknown keys | Preserved verbatim. |
| Key order | Preserved (the `_FmEntry` ordered representation). |
| `type` value | Preserved exactly. |
| Recommended keys when set | Preserved exactly. |
| List values | Order preserved. |
| Inline vs block style | The dumper emits block style for nested structures; simple scalars stay inline. |
| Trailing whitespace | Normalised to a single trailing newline. |

# Validation

Run [`scripts/okf-loom validate`](cli.md#validate) to surface frontmatter findings.
Useful check subsets:

```bash
scripts/okf-loom validate path/to/bundle --checks concept_required_keys,concept_recommended_keys
scripts/okf-loom validate path/to/bundle --checks timestamp_validity
scripts/okf-loom validate path/to/bundle --checks metadata_validity
scripts/okf-loom validate path/to/bundle --profile producer   # recommended keys → ERROR
```

The check categories that touch frontmatter:

| Check | Category |
|---|---|
| `concept.missing_type` | `concept_required_keys` |
| `concept.missing_recommended_keys` | `concept_recommended_keys` |
| `concept.unknown_type` | `concept_recommended_keys` (WARNING) |
| `concept.bad_timestamp` | `timestamp_validity` |
| `concept.bad_generated`, `concept.bad_verified`, `concept.bad_sources`, `concept.bad_usage_window`, `concept.bad_status`, `concept.bad_computation`, `concept.missing_runtime` | `metadata_validity` (WARNING) |
| `concept.tag_normalization` | `tag_normalization` |

# Authoring tools

The CLI mutator verbs go through the same parse → modify → serialize
pipeline, so behaviour is identical whether you hand-edit or use a verb:

- [`scripts/okf-loom write-concept`](cli.md#write-concept) — create or update a concept.
- [`scripts/okf-loom set-frontmatter`](cli.md#set-frontmatter) — set one frontmatter key.
- [`scripts/okf-loom link-add`](cli.md#link-add) — append a markdown link (and optional typed relation).
- [`scripts/okf-loom entity-add`](cli.md#entity-add) — append an entity.

See [/how-to/author_with_verbs.md](/how-to/author_with_verbs.md) for
the workflow, and [/tutorials/first_bundle.md](/tutorials/first_bundle.md)
for the first-bundle walk-through.

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [cli.md](cli.md) — CLI command reference.
- [links.md](links.md) — link forms and graph edges.
- [capabilities.md](capabilities.md) — capability registry and auto-activation.
- [config_yaml.md](config_yaml.md) — bundle-root config (separate file, separate contract).
- [/explanation/architecture.md](/explanation/architecture.md) — module-by-module design.
- [/reference/spec.md](/reference/spec.md) — the current okf-loom contract.
- Upstream OKF SPEC §4.1 — the base wire-format frontmatter contract.
