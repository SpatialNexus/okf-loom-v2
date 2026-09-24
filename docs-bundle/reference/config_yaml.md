---
type: Reference
title: okf-loom.config.yaml reference
description: Every `okf-loom.config.yaml` key — `bundle.*`, `viewer.*`, `search.*`, `discover.*`, `validate.*`, `studio.*` — with type, default, and behaviour. Path-bearing fields are bundle-relative; unknown keys are preserved in `OkfConfig.raw`.
resource: /reference/config_yaml.md
tags: [config, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# `okf-loom.config.yaml` reference

A bundle MAY ship an `okf-loom.config.yaml` at its root (current spec §5)
to set viewer / search / discover / validate / studio defaults. **Absence ⇒ all
defaults** — the file is purely additive; nothing breaks without it.
[`scripts/okf-loom init`](cli.md#init) writes a commented-defaults template
automatically; [`scripts/okf-loom bootstrap`](cli.md#bootstrap) does not.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

The canonical filename is `okf-loom.config.yaml` (single constant in
`config.py`; flip it to relocate).

# Top-level shape

## Portable review destination

```yaml
viewer:
  review_base_url: https://docs.example.com/studio/team/
```

`viewer.review_base_url` is optional (`null` by default), never inferred from a
running session. It is an HTTP(S) deployment base, not a share URL containing
authority. It allows a path prefix and normalizes one trailing slash. Userinfo,
any query/fragment (even empty), unsafe schemes, percent escapes, backslashes,
non-ASCII/control/whitespace, dot segments, repeated path slashes, invalid ports
and malformed forms fail configuration/build admission. Errors do not echo the
configured secret-bearing value. No upload is performed.

Static and single-file exports expose the validated value in the
`okf-review-bootstrap` JSON hook. Clients append the selected canonical `.md`
document and separately encode its fragment. Without a base, exports remain
read-only and carry an explanation that comments require live Studio. The base
grants no authority: the destination enforces its own permission mode.

```yaml
bundle:   { … }   # markdown scanning excludes
viewer:   { … }   # display + override settings
search:   { … }   # search-mode default
discover: { … }   # discovery noise controls
validate: { … }   # validation profile + broken-link policy
studio:   { … }   # live collaborative studio
```

The six top-level keys are the only recognised top-level keys.
Unknown top-level keys are preserved verbatim in `OkfConfig.raw`
for forward compatibility.

# Path-bearing fields (Loom containment contract)

The following fields MUST be bundle-relative. Absolute paths and
`..`-escaping paths are rejected fail-closed at load time:

- `viewer.override_dir`
- `viewer.extension_css`
- `viewer.extension_js`
- `studio.session_dir`

The check reuses Loom's containment guard (`log._resolve_within_bundle`):
it resolves the candidate against the bundle root and confirms
containment via `relative_to`. Empty values are also rejected.

# `bundle.*`

Controls which `*.md` files are part of the bundle when scanning
(`Bundle.load`, the serve watcher, `okf import`). The scanner walks the
tree top-down and PRUNES excluded directories, so serving or validating a
real workspace root does not sweep `node_modules`, nested cloned repos, or
virtualenvs in as thousands of missing-`type` pseudo-concepts — and never
pays the cost of descending into them.

| Key | Type | Default | Description |
|---|---|---|---|
| `exclude` | list of strings (or one string) | `[]` | Gitignore-style patterns skipped during the scan. Sits above the defaults and `.gitignore`: a negation (`"!.docs/"`) re-includes a path those groups excluded (git-style: not from under a pruned directory). |
| `include` | list of strings (or one string) | `[]` | The add-back lever. Positive patterns that beat EVERY exclusion source — defaults, `.gitignore`, `exclude`, and the structural nested-repo prune — and can reach inside pruned directories. Negations are rejected fail-closed. |
| `respect_gitignore` | bool | `true` | Honour `.gitignore` files — the bundle root's AND nested ones, each scoped to its own directory — during the scan. |

Three exclusion rule groups apply, later group wins, and within a group
the last matching rule wins (git semantics):

1. **Built-in defaults**: hidden directories (`.git`, `.okf-loom`,
   `.venv`, …), `node_modules/`, `__pycache__/`, `venv/`,
   `bower_components/`. Hidden *files* still load. Any non-root directory
   containing a `.git` entry (nested clone / submodule / worktree) is
   pruned — a structural rule, overridable only by `include`.
2. **`.gitignore` rules** (when `respect_gitignore` is on).
3. **`exclude` patterns** (this section).

`include` sits above all three. Two forms:

* **Directory form** (`"vendor-repo/"`, or a wildcard-free path like
  `"node_modules/my-pkg/docs"`): revives that directory — the scanner
  reaches it through pruned ancestors, then scans the subtree as normal
  bundle content. Exclusion rules that were overridden at the revived
  directory (e.g. an anchored `node_modules/**` in `.gitignore`) stay
  overridden beneath it; unrelated rules (hidden-dir default, basename
  rules like `*.tmp.md`, deeper excludes) keep applying inside. This is
  the recommended form.
* **Glob form** (`"vendor-repo/**"`): force-includes exactly what it
  matches, everything beneath — defaults included. Broad `**` on a big
  tree is a deliberate, paid-for traversal.

Include patterns without a `/` (basename form, `"keep.md"`) apply wherever
the scan already reaches but never open pruned directories — pulling
something out of a pruned tree requires naming its path.

Pattern dialect (practical gitignore subset): `*` / `?` / character
classes never cross `/`; `**` spans segments; trailing `/` = directory-only;
a `/` anywhere else anchors the pattern to the bundle root (or the nested
`.gitignore`'s directory); leading `!` = negation (exclude only); `\`
escapes; `#` comments.

`exclude`/`include` entries are match rules, not filesystem paths, so the
§4.5 containment guard does not apply (a leading `/` is anchor syntax;
matching is always against root-relative paths). Non-string entries are
rejected fail-closed.

An unparseable config never makes the bundle unloadable: `Bundle.load`
warns (`config.unparseable`) and scans with the built-in defaults.

```yaml
bundle:
  exclude:
    - "drafts/**"                    # keep WIP notes out of validate/serve
    - "*.generated.md"               # tool output, not concepts
    - "!.docs/"                      # re-include a hidden dir the defaults pruned
  include:
    - "vendor-repo/"                 # nested git clone we DO want, scanned normally
    - "node_modules/my-pkg/docs/"    # one docs dir pulled out of node_modules
  respect_gitignore: true
```

Implementation: `okf_loom/ignore.py` (stdlib-only leaf module).

# `viewer.*`

| Key | Type | Default | Description |
|---|---|---|---|
| `title` | string \| null | `null` | Viewer page `<title>`. `null` ⇒ no override; the viewer falls back to the bundle display name. |
| `override_dir` | string (bundle-relative path) | `.okf-loom/viewer` | Directory holding template / static / palette overrides. |
| `extension_css` | string (bundle-relative path) | `.okf-loom/viewer/extension.css` | Optional CSS injected into every page. |
| `extension_js` | string (bundle-relative path) | `.okf-loom/viewer/extension.js` | Optional JS injected into every page (subject to the active-code gate). |
| `allow_active_code` | bool | `false` | Bundle-side gate for viewer overrides + plugins. The operator must ALSO consent via `--allow-active-code` / `OKF_LOOM_ALLOW_ACTIVE_CODE` (two-layer gate). |

## Boolean coercion

`allow_active_code` (and every other bool field) is coerced
fail-closed. Accepted tokens (case-insensitive, whitespace-trimmed):

| True | False |
|---|---|
| `true`, `yes`, `on`, `1` | `false`, `no`, `off`, `0`, `""` |

Any other value (e.g. `"maybe"`, an int, a list) raises
`OkfConfigError`. This is deliberately strict: a quoted `"false"`
must NOT silently enable active code.

# `search.*`

| Key | Type | Default | Description |
|---|---|---|---|
| `default_mode` | enum | `lexical` | Mode used when no `--mode` is passed. One of `lexical`, `semantic`, `hybrid`, `tag`, `entity`, `relation`. |

Unknown values are rejected fail-closed (no silent fallback to a
different backend). See [search_modes.md](search_modes.md).

# `discover.*`

Bundle-specific discovery suppressions. These are editorial noise controls;
they do not affect validation, search, graph construction, or authored OKF
data.

| Key | Type | Default | Description |
|---|---|---|---|
| `suppress_phrases` | list of strings (or one string) | `[]` | Case-insensitive phrases to suppress from `unlinked_mentions`. Use for labels that are noisy in this bundle but may be useful elsewhere. |
| `suppress_pairs` | list of `{source, target}` mappings | `[]` | Suppress one source→target unlinked-mention pair. `source` and `target` may be concept ids (`project-a/spec`) or bundle-absolute concept links (`/project-a/spec.md`). |

Example:

```yaml
discover:
  suppress_phrases:
    - architecture
    - product spec
  suppress_pairs:
    - source: /project-a/product-spec.md
      target: /project-b/product-spec.md
```

Suppressed suggestions remain visible in JSON under `suppressed`, with
`detail.suppression_reasons` set to `configured_phrase` or `configured_pair`.

# `validate.*`

| Key | Type | Default | Description |
|---|---|---|---|
| `default_profile` | enum | `spec` | Profile used when no `--profile` is passed. One of `spec`, `producer`, `loose`. |
| `fail_on_broken_links` | bool | `false` | Promote `link.broken` findings to ERROR. |

| Profile | Behaviour |
|---|---|
| `spec` | Default. Only `type` is required; everything else is WARNING. |
| `producer` | `spec` + `concept.missing_recommended_keys` promoted to ERROR. |
| `loose` | `spec` except `concept.missing_type` downgraded to WARNING. |

Unknown `default_profile` values are rejected fail-closed.
See [cli.md § validate](cli.md#validate).

# `studio.*`

A plain `scripts/okf-loom serve` enables the implemented live-update and
commenting surfaces without unlock flags. Some accepted keys are reserved for
external-agent policy and currently have no server-runtime effect.

| Key | Type | Default | Description |
|---|---|---|---|
| `edit` | bool | `true` | Interactive studio (commenting/directing + live). `--no-edit` flips to a read-only kiosk. |
| `live` | bool | `true` | SSE live updates on. |
| `enrich_offer` | bool | `true` | Reserved external-agent metadata; not consumed by the current server runtime. |
| `auto_enrich` | bool | `true` | Reserved external-agent metadata; not consumed by the current server runtime. |
| `constraints` | mapping | `{}` | Studio write-funnel path/group limits plus external-agent metadata; see exact coverage below. |
| `debounce_ms` | int | `600` | Reserved studio configuration; the current serve watcher does not consume it. Use the watch command's `--debounce-ms` option for watcher debounce. |
| `session_dir` | string (bundle-relative path) | `.okf-loom/session` | Where session state (events, directives, history) lives. |
| `log_edits` | bool | `true` | Append an upstream §9 `log.md` entry per agent write / resolved comment. |
| `max_sse_clients` | int | `32` | Cap on concurrent SSE streams. Returns 503 when full. |
| `allowed_hosts` | list of strings | `[127.0.0.1, localhost]` | Origin/Host allow-list for the cross-origin guard. Never empty (defaults to loopback on unset). |
| `theme` | enum | `auto` | `auto` / `technical-light` / `technical-dark` / `swiss-light` / `swiss-dark` / `purple-light` / `purple-dark` / `coffee-light` / `coffee-dark`. Seeded into the served runtime by `theme.js` when no saved user preference exists: `auto` keeps the current/default family and follows the OS light/dark colour mode; a concrete value pins that family + mode. Appearance exposes family and mode as separate preferences: Technical remains the application default and Coffee is the editorial showcase. It also stores a separate compact (default) or comfortable interface-density preference without changing prose measure or minimum targets; interface borders default to off (flat) until the reader turns them on. A saved user preference always wins; the configured value is never persisted on the user's behalf. |
| `events_max_bytes` | int | `8388608` (8 MiB) | `events.jsonl` rotation cap (active file size). |
| `events_keep` | int | `7` | Number of rotated `events.jsonl` files to keep. |

## `allowed_hosts`

Both `Origin` (when present) and `Host` headers are checked against
this list (defense in depth against DNS-rebinding). The list is
never empty: a missing/empty value defaults to
`("127.0.0.1", "localhost")`. A non-loopback bind requires
`scripts/okf-loom serve --public --public-ack` (or interactive y/N ack).

## `constraints`

The mapping is preserved, but coverage is deliberately narrower than a general
policy engine:

- `forbidden_paths`: when supplied as a nonempty list, Studio's save funnel
  matches each pattern (converted to a string) with Python `fnmatch` against
  the concept id, without the `.md` suffix. A match refuses the write. Index
  writes routed through Studio use path-derived ids such as `reference/index`.
- `max_edits_per_action`: a positive integer caps the **unique concept members
  of a group**, not the number of write calls or all edits in a session.
  Re-editing a member does not consume another slot. It applies only when
  `group_id` is present. Grouped transactions preflight/reserve the complete
  member set, including grouped index writes, before mutation.
- `propose_only: true` enables explicit `Studio.post_suggestion` appends to
  the legacy `proposals.jsonl` stream. It does **not** intercept direct saves,
  require review, or activate experimental Proposal / Diff Review.
- Other entries remain external-agent metadata. This mapping has no closed
  per-key policy schema; use the documented list/integer/boolean shapes, not
  coercion as policy validation.

These limits cover writes routed through Studio (including connected CLI
mutators and the direct HTTP apply path), not arbitrary filesystem writes or
the non-Studio CLI path. The separate experimental proposal backend does not
use these values as authorization or mandatory-review policy; see its
[extension contract](/reference/proposal_diff_review.md). Comprehensive
structured denied-action audit coverage across routes is not established here.
Changing enforcement or audit/retention policy requires a separate decision.

## `session_dir`

Must be bundle-relative. An absolute path raises `OkfConfigError`;
a `..`-escaping path is rejected by the §4.5 guard. The directory
holds `events.jsonl`, `directives.jsonl`, `history/`, `.token`.

# Unknown keys

Unknown keys (top-level AND section-internal) are preserved verbatim
in `OkfConfig.raw`. The same round-trip principle as upstream §4.1 applies to
frontmatter applies here: a newer producer's config does not lose
information when consumed by an older toolkit.

```python
from okf_loom.config import OkfConfig

cfg = OkfConfig.load("path/to/bundle")
cfg.raw                  # dict of unknown top-level + section-internal keys
cfg.raw.get("acme_xxx")  # forward-compat read of an unknown key
```

# Loading

| Situation | Behaviour |
|---|---|
| File absent | All defaults (`OkfConfig()`). |
| File empty / comment-only | All defaults (treated as empty mapping). |
| File unparseable | `OkfConfigError` (subclass of `ValueError`). |
| Top level not a mapping | `OkfConfigError`. |
| Path field escapes bundle root | `OkfConfigError`. |
| Unknown enum value (`search.default_mode`, `validate.default_profile`, `studio.theme`) | `OkfConfigError`. |
| Alias/merge-key bomb | `OkfConfigError` (DoS guard via `_NodeLimitedSafeLoader`). |

The loader uses the same node-counting YAML loader as frontmatter
parsing, so the same DoS controls cover both surfaces.

# Library

```python
from okf_loom.config import OkfConfig, OkfConfigError

try:
    cfg = OkfConfig.load("path/to/bundle")
except OkfConfigError as e:
    ...  # unparseable / bad path / unknown enum

cfg.bundle.exclude              # () — gitignore-style patterns
cfg.bundle.include              # () — add-back patterns (beat every exclusion)
cfg.bundle.respect_gitignore    # True
cfg.viewer.title                # str | None
cfg.viewer.allow_active_code    # bool
cfg.search.default_mode         # "lexical"
cfg.validate.default_profile    # "spec"
cfg.validate.fail_on_broken_links  # False
cfg.studio.edit                 # True
cfg.studio.allowed_hosts        # ("127.0.0.1", "localhost")
cfg.studio.session_path         # ".okf-loom/session" (normalized)
cfg.raw                         # {"unknown_top": ..., "viewer": {"unknown_sub": ...}}
```

`OkfConfig` and its section dataclasses are **frozen**; construct a
new one to change a value.

# Relationship to other config surfaces

| Surface | Scope | Format |
|---|---|---|
| `okf-loom.config.yaml` | Bundle defaults (this doc) | YAML at bundle root. |
| Bundle-root `index.md` frontmatter | Upstream §8/§12 `okf_version`, plus Loom `okf_extensions` and boolean `generated` ownership | Frontmatter. |
| Concept frontmatter | Per-concept metadata | SPEC §4.1 (see [frontmatter.md](frontmatter.md)). |
| `<bundle>/.okf-loom/viewer/config.json` | Viewer-only display overrides (name, default_layout, theme) | JSON. |

`okf-loom.config.yaml` is the authoritative bundle-level config; the
viewer's `config.json` is a narrower display-only override.

# Authoring tools

| Goal | Tool |
|---|---|
| Scaffold a new bundle with a commented-defaults config | [`scripts/okf-loom init --bundle <path>`](cli.md#init). |
| Scaffold an empty bundle without a config | [`scripts/okf-loom bootstrap <dest>`](cli.md#bootstrap). |
| Validate the config | [`scripts/okf-loom validate <bundle>`](cli.md#validate) (the loader runs at bundle load; an unparseable config raises before any check). |
| Inspect resolved config | `OkfConfig.load(...)` in Python. |

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [frontmatter.md](frontmatter.md) — concept-level frontmatter (different contract).
- [cli.md § init](cli.md#init) / [cli.md § bootstrap](cli.md#bootstrap) — scaffolding commands.
- [cli.md § validate](cli.md#validate) — profiles and `--fail-on-broken-links`.
- [search_modes.md](search_modes.md) — `search.default_mode` values.
- [http_routes.md](http_routes.md) — `studio.allowed_hosts`, `studio.max_sse_clients`, `studio.session_dir` in route behaviour.
- [comment_lifecycle.md](comment_lifecycle.md) — `studio.events_max_bytes`, `studio.events_keep`, staleness TTLs.
- [capabilities.md](capabilities.md) — `viewer.allow_active_code` gates viewer plugin discovery.
- [/explanation/architecture.md](/explanation/architecture.md) — module map.
- [`/reference/spec.md`](/reference/spec.md) — current config and studio contract.
- `okf-loom/scripts/okf_loom/config.py` — authoritative source.
