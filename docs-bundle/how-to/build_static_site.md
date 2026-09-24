---
type: How-to
title: Build a static site
description: Render an OKF bundle to a portable static site with the build command, choosing the SPA, static, or single-file target, and optionally embedding it inside another site.
resource: /how-to/build_static_site.md
tags: [build, render, static-site, viewer]
timestamp: "2026-06-29T00:00:00Z"
---

# Build a static site

## Optional live review destination

If you already operate a Studio destination, configure a credential-free base
before building either static or single-file output:

```yaml
viewer:
  review_base_url: https://docs.example.com/studio/team/
```

Do not paste a credential, signed query, or fragment into the base; validation
rejects those forms. The exporter provides `okf-review-bootstrap` metadata for
the Review in Studio action, using each document's canonical `.md` route and
heading fragment. This does not start a server, upload content, grant comment
authority or embed comments. Without configuration the artifact remains a useful
read-only document; its metadata explains that comments require a live session.
See the [configuration contract](/reference/config_yaml.md#portable-review-destination).

This guide takes a bundle on disk and ships it as a static site you can
host anywhere — GitHub Pages, Netlify, an intranet, or a `file://` URL.

You already have a bundle that passes validation.
If you do not, [Author your first bundle](/tutorials/first_bundle.md)
gets you there.

# Step 1: Choose a build target

The build command ships three targets, each tuned for a different shipping
context.

| Target | When to use |
|---|---|
| `spa` | Quartz-style default for a published reading experience. Client-side rendering with the portable search and context workbench. |
| `static` | Server-rendered, JS-optional, printable, SEO- and airgapped-friendly; JavaScript adds portable search and context preparation. |
| `single-file` | One self-contained HTML with the graph and retrieval corpus embedded. Email it, attach to a ticket, open from `file://`. |

Pick `static` when you want a hostable site that still works without
JavaScript.
Pick `spa` when you want the smoothest reading experience and do not mind
requiring JS.
Pick `single-file` when the artifact has to travel as one file.

# Step 2: Build the site

Run the checkout-local build command with the target and an output directory.

```bash
scripts/okf-loom build path/to/bundle --target static --out dist/
```

The `dist/` directory now contains the full site: rendered HTML pages, a
copy of the graph data, viewer assets, canonical no-JS concept contexts, and a
schema-versioned retrieval corpus.

For `static` and `spa`, the corpus lives under `dist/__data/`:

```text
__data/passages-manifest.json
__data/passages-000000-<content-hash>.json
__context/<concept-id>.txt
```

The fixed-name manifest identifies schema version 1, the retrieval algorithm,
the bundle revision, and every chunk.
Chunk names are content-addressed, and a client validates schema, revision,
identity, count, and hash before publishing a complete corpus.
Mixed revisions, missing chunks, malformed data, and unsupported schemas fail
closed with an unavailable/retry state; they never produce lexical fallback
results from a partial corpus.

The other targets take the same flags:

```bash
scripts/okf-loom build path/to/bundle --target spa --out dist/
scripts/okf-loom build path/to/bundle --target single-file --out dist/
```

`build --out` is a directory for every target. The single-file build writes
`dist/index.html`, not a caller-named HTML file. Use `render --out viz.html`
below when you want to choose the filename.

## Optional offline feedback notes

To let readers of a static or single-file export write page-level feedback
without any server, add `--include-portable-feedback`:

```bash
scripts/okf-loom build path/to/bundle --target static --out dist/ --include-portable-feedback
```

Each concept page gets a **Write feedback note** button. The reader writes a
note, optionally adds a quote, a name, and the time, and downloads it as a
`.okf-feedback.json` file or copies the JSON. Nothing is sent anywhere. Each
note is bound to the exact source bytes of the page it was written on, so a
local Studio operator's [import](/how-to/import_portable_feedback.md) can refuse a note whose page has since changed.
Pages whose source changed while the build ran are skipped with a warning and
counted in the build output. Without the flag the output is unchanged, and the
`spa` target does not accept it.

## Optional managed output

By default a `static` or `spa` build replaces its output directory wholesale,
even if you put other files there. To have the build refuse to overwrite
anything it did not write, opt in to managed output:

```bash
scripts/okf-loom build path/to/bundle --target static --out dist/ --output-policy managed
```

The build writes `dist/.okf-export.json`, which lists every file and directory
it published. The next managed build replaces `dist/` only if everything in it
is still exactly what that manifest lists. Switching between `--target static`
and `--target spa` is fine. `single-file` does not support managed output.

If the build refuses, it exits `2`, prints the reason code and the first
offending path, and changes nothing:

- `export.ownership.entry_unknown`: you or another tool added a file. Move it
  out of `dist/`.
- `export.ownership.entry_modified`: a published file was edited or deleted.
  Rebuild after restoring or discarding the change deliberately.
- `export.ownership.entry_symlink` / `entry_special`: remove the symlink,
  FIFO, socket, or device from `dist/`.
- `export.ownership.manifest_malformed` / `manifest_unsupported`: the manifest
  is damaged or from another version. Inspect `dist/`, then delete or relocate
  it deliberately.
- `export.ownership.unmanaged_nonempty`: `dist/` has files but no manifest.
  Use an empty or new directory, or see adoption below.

To start managing an existing default build, add `--adopt-output`:

```bash
scripts/okf-loom build path/to/bundle --target static --out dist/ --output-policy managed --adopt-output
```

Adoption succeeds only if `dist/` is byte-identical to the build it is about
to publish, so nothing is lost. Otherwise it refuses with
`export.ownership.adopt_mismatch`. Repeated builds of an unchanged bundle are
byte-identical, but a concept whose `stale_after` passed since the earlier
build, or a changed recent-change history, changes the output. In that case,
inspect `dist/` and replace it deliberately. A default (`replace`) build into a
managed directory drops the manifest, so return to managed mode with
`--adopt-output`.

If a build is killed or loses power mid-publication, the previous output can be
left in a `.okf-recovery-*` directory next to `dist/`. Builds never adopt or
clean it. Inspect it, then restore or delete it yourself. After a power loss, a
torn manifest is refused as `manifest_malformed`.

## Optional fully offline output

By default, pages load Mermaid, KaTeX, highlight.js, and the Cytoscape graph
libraries from a pinned CDN. For an air-gapped network, or any reader who must
not contact a third party, add `--assets offline`:

```bash
scripts/okf-loom build path/to/bundle --target static --out dist/ --assets offline
scripts/okf-loom render path/to/bundle --out viz.html --assets offline
```

The build ships byte-verified copies of the same library versions. `static`
and `spa` place them under `dist/__static/vendor/`, and `single-file` and
`render` embed them. It refuses to write anything if a vendored file fails its
integrity check. The output is about 6.6 MB larger, and a single file can grow
by up to about 7.1 MB. It includes each library's licence,
`THIRD-PARTY-NOTICES.txt`, and `BUNDLED-DEPENDENCY-NOTICES.txt` (the full
licence texts of the code bundled inside those libraries), all embedded for a
single file, so keep those files when you redistribute the site. Offline mode
cannot be combined with `--allow-active-code`.

Maintainers who bump a vendored library version do it in one reviewed change:
update the pinned URLs and SRI values in the code, run
`scripts/generate_bundled_notices.py --write` and review the regenerated
`BUNDLED-DEPENDENCY-NOTICES.txt` (and `THIRD-PARTY-NOTICES.txt`), then run
`scripts/fetch_vendor_assets.py --init-lock`, then
`python tests/regenerate_offline_cdn_golden.py` to refresh the CDN render
golden. The first two scripts and the golden step need network access;
`scripts/fetch_vendor_assets.py --check` and the tests do not.

A single offline file works completely from `file://`. A static offline site
loads its libraries from its own files even from `file://`, but the graph and
search fetch their data and need an HTTP server, just as in the default mode.
An SPA site always needs an HTTP server that serves it at the site root: its
pages use root-absolute `/__static/...` paths, which `file://` cannot resolve.

# Step 3: Preview the build locally

Any static file server works.

```bash
python3 -m http.server --directory dist 8000
```

Open `http://127.0.0.1:8000/` and click around to confirm the render
before you ship.

Open the Search page and verify the six visible modes (**Best overall**, **Exact
wording**, **Similar meaning**, **Tagged with**, **Mentions entity**, and
**Connected by relation**), Type/Tag and mode-specific filters, compact excerpts,
and the complete citations and ranking reasons under **Evidence and context**.
Prepare and copy a Context Pack from the result set, from an individual
concept result, and from a selected graph node.
These JavaScript-enabled portable surfaces use the validated corpus and the
same deterministic schema-v1 pack contract as the live server.

Then disable JavaScript for the `static` target and verify the honest
fallback: pages, type/tag browsing, and canonical per-concept Markdown context
remain available.
Arbitrary search, exact rendered-selection mapping, and graph-neighbourhood
context preparation require JavaScript in a static build.

For a quick single-file preview, `render` writes one self-contained
`viz.html` without invoking the full build pipeline.

```bash
scripts/okf-loom render path/to/bundle --out viz.html
```

`render` is the right tool when you want one portable artifact and do
not need the multi-file site.
The single file embeds the complete normalized passage corpus and supports
all six search modes, evidence, per-result context, result-set context, and
graph-neighbourhood context without network requests.
That convenience also makes file size grow with the bundle, so prefer the
chunked `static` or `spa` target for large bundles.

# Step 4: Handle active code (overrides and plugins)

If the bundle ships viewer overrides or plugins that run JavaScript —
custom templates, a `ViewerPlugin`, or `viewer.allow_active_code: true` in
`okf-loom.config.yaml` — the build refuses to execute them unless you give
operator consent.

```bash
scripts/okf-loom build path/to/bundle --target spa --out dist/ --allow-active-code
```

The two-layer gate is intentional: the bundle declares it wants active
code, and the operator explicitly allows it at build time.
Without consent, the build still succeeds but the overrides and plugins
are inert.

Set `OKF_LOOM_ALLOW_ACTIVE_CODE=1` in the build environment to consent
non-interactively, for example in a CI job.

# Step 5: Ship the site

The `dist/` directory is fully self-contained; copy it anywhere.

For GitHub Pages, drop it into the `gh-pages` branch or point Pages at the
build output of your CI workflow.
For Netlify or Vercel, point the deploy command at `dist/`.
For an airgapped intranet, copy the directory onto the static file server
of your choice.

Because the `static` target is JS-optional, the site renders legibly even
in a restricted browser.
Portable retrieval adds manifest, chunk, context, and JavaScript assets, so
expect larger output and longer builds than a reading-only render.
Measure your own bundle and deployment limits rather than treating the
single-file target as suitable for arbitrarily large corpora.

# Step 6: Embed the site inside another site

The `single-file` target is the easiest embed: copy `dist/index.html` into
your site's `/okf/` directory and use an `<iframe>` (or use the `viz.html`
filename if you chose `render`).

```html
<iframe src="/okf/index.html" width="100%" height="800" frameborder="0"></iframe>
```

For a deeper integration — your own chrome around the viewer, a custom
build step, or running the live server inside another process — see
[Embed the viewer in an agent harness](/how-to/embed_in_harness.md).

# Step 7: Automate the build in CI

Pair the build with the validate gate so a broken bundle never ships.

```yaml
# .github/workflows/okf-build.yml
name: OKF build
on:
  push:
    branches: [main]
    paths: ['docs-bundle/**']
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: scripts/okf-loom validate docs-bundle --strict --profile producer
      - run: scripts/okf-loom build docs-bundle --target static --out dist/
      - uses: actions/upload-pages-artifact@v3
        with:
          path: dist/
```

See [Validate a bundle in CI](/how-to/validate_in_ci.md) for the full
validate workflow.

# End state

You now have a repeatable build that:

- Turns a bundle into a hostable site in one command.
- Picks the right target for the shipping context.
- Honours the active-code gate when overrides or plugins are present.
- Ships deterministic evidence and Context Packs without a database,
  embeddings, an LLM, a framework, or a bundler dependency.

# See also

* [Validate a bundle in CI](/how-to/validate_in_ci.md) — gate the build behind strict validation.
* [Embed the viewer in an agent harness](/how-to/embed_in_harness.md) — deeper integration shapes.
* [CLI command reference](/reference/cli.md) — every flag on `build` and `render`.
* [HTTP server routes](/reference/http_routes.md) — the routes the live server and SPA share.
* [Search modes](/reference/search_modes.md) — six deterministic ranking modes and their filters.
* [Context Packs](/reference/context_packs.md) — schema, citations, budgets, and portable-surface limits.
* [okf-loom architecture](/explanation/architecture.md) — why one content model renders to many targets.

Return to [How-to guides](/how-to/index.md).
