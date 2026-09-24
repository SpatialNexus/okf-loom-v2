# Command reference

The runtime lives in `scripts/okf_loom/`. Run it from a checkout with the
checked-in `scripts/okf-loom` helper; do not assume an installed package or console script.

## Helper

From a parent workspace:

```bash
okf-loom/scripts/okf-loom validate okf-loom/docs-bundle --strict
```

From inside `okf-loom/`:

```bash
scripts/okf-loom validate docs-bundle --strict
```

## Common commands

```bash
scripts/okf-loom --help
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom info docs-bundle
scripts/okf-loom graph-quality docs-bundle
scripts/okf-loom search docs-bundle "current spec" --mode lexical
scripts/okf-loom context docs-bundle "current spec" --mode hybrid --budget-tokens 6000 --neighbors 1 --limit 20 --format markdown
scripts/okf-loom context docs-bundle --concept reference/spec --format json
scripts/okf-loom serve docs-bundle --no-open
scripts/okf-loom serve docs-bundle --no-open --tunnel   # + public https URL (needs cloudflared)
scripts/okf-loom build docs-bundle --target static --out /tmp/okf-docs-site
scripts/okf-loom build docs-bundle --target static --out /tmp/okf-docs-site --output-policy managed
```

`--output-policy managed` (static/spa only) writes a `.okf-export.json`
ownership manifest and refuses, with exit `2` and no changes, to replace output
containing unknown, edited, or deleted files, symlinks, or special files. Add
`--adopt-output` to take over an unmanaged directory only when it is
byte-identical to the new build. The default `replace` policy is unchanged.

## Sharing a live studio

`serve --tunnel` starts a Cloudflare quick tunnel to a dedicated,
generation-bound loopback ingress, prints the public
`https://…trycloudflare.com` URL, and leaves the local/admin allowlist
unchanged. Public access is read-only by default: mutation routes reject
requests, mutation controls are unavailable, and public HTML contains no
mutation token. Local studio editing is unchanged. The tunnel dies with the
process. Anyone with the link can READ the bundle — say so when you hand the
URL over. Requires `cloudflared` on PATH; without it the command warns and
serves local-only.

For comments and replies without content or comment-row mutation, use
`serve ... --tunnel --allow-tunnel-comment`, or
`tunnel <bundle> --allow-tunnel-comment` when attaching. It is mutually
exclusive with `--allow-tunnel-edit` and is rejected with `--no-edit` or
Studio configuration that disables editing; no presence, agent, proposal, or
admin authority is granted.

To let link holders comment and mutate intentionally, use
`serve ... --tunnel --allow-tunnel-edit`. This is a dangerous capability
change: anyone with the URL receives mutation authority, so share it only with
people you trust. `--allow-tunnel-edit` without `--tunnel`, or together with
`--no-edit` or Studio configuration that disables editing, is rejected.

Forgot `--tunnel`? Attach one to the RUNNING session — no restart, no lost
session token or undo history:

```bash
scripts/okf-loom tunnel docs-bundle                       # read-only public URL
scripts/okf-loom tunnel docs-bundle --allow-tunnel-comment   # anyone with URL may comment/reply only
scripts/okf-loom tunnel docs-bundle --allow-tunnel-edit   # anyone with URL may mutate
scripts/okf-loom tunnel docs-bundle --status              # current URL (or none)
scripts/okf-loom tunnel docs-bundle --stop                # detach and revoke public ingress/token
```

## Partial body updates

Change one block of a concept without touching the rest (no whole-body
`write-concept --force`, no staging copies):

```bash
# Replace ONE section (subsections included); fail-closed on missing/ambiguous headings.
scripts/okf-loom update-section --bundle docs-bundle --id reference/cli --heading "## Examples" --body-file /tmp/frag.md
# Append to a section instead of replacing; create it if absent.
scripts/okf-loom update-section --bundle docs-bundle --id reference/cli --heading "Notes" --body "extra line" --append --create-if-missing
# Exact textual patch; must match exactly once (or pass --all).
scripts/okf-loom replace-text --bundle docs-bundle --id reference/cli --old "old exact text" --new "new exact text"
```

## Full surface

- `validate`, `info`, `graph`, `graph-quality`, `search`, `context`
- `discover`, `plan`, `repair`, `update`
- `write-concept`, `set-frontmatter`, `link-add`, `entity-add`,
  `update-section`, `replace-text`
- `index`, `log`, `init`, `bootstrap`, `import`
- `serve`, `tunnel`, `wait`, `watch`, `token`, `comment-claim`,
  `comment-reply`, `comment-resolve`, `comment-list`, `presence`
- `render`, `build`, `capabilities`, `upgrade`
- `proposal` — experimental opt-in Proposal Review commands; the local Studio
  Proposals panel is inspection-only. See
  [Proposal Diff Review](../docs-bundle/reference/proposal_diff_review.md).

Most read/report commands accept `--format json`. `wait` prints one JSON work
item (with a `queue` field showing other still-open comments). `watch --emit
jsonl` tails change events. `write-concept` defaults `resource` and truthful tool
`generated` metadata on create (opt out with `--no-defaults`); title, description,
and tags still need real content. Explicit `--timestamp` preserves legacy input.

`upgrade <bundle>` / `--preview` / `--check` are read-only. `--apply` explicitly
opts into conservative 0.1 → 0.2 changes; `--actor` asserts the original producer,
not the migrating agent. Partial reports can exit zero. Review the
[migration guide](../docs-bundle/how-to/migrate_spec_version.md) for optional
revision CAS, per-file atomicity, declaration-last ordering, and interruption reruns.

`search` and `context` support `lexical`, `semantic`, `hybrid`, `tag`,
`entity`, and `relation` modes. Search defaults to 20 results (maximum 100).
Context defaults to hybrid mode, a 6000-token budget, one graph-neighbor hop,
an item limit of 20, and Markdown output; accepted bounds are 128–50000 tokens,
0–2 hops, and 1–100 items. Use `--format json` for schema-versioned
automation. Invalid bounds and mode-inapplicable filters fail closed. See the
[Context Packs reference](../docs-bundle/reference/context_packs.md) for the
stable citation and output contract.

## Dependencies

Runtime requires Python 3.11+.
PyYAML is preferred when present, but normal docs validation does not require a primary install step: the skill ships a conservative built-in YAML fallback that fails closed on unsupported advanced YAML.

Test/proof environments can add tools explicitly, without installing this repo
as a package:

```bash
python -m pip install pytest playwright
python -m playwright install chromium
PYTHONPATH=scripts pytest
```
