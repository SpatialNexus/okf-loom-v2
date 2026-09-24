# OKF overview

OKF (Open Knowledge Format) represents knowledge as plain Markdown files with
YAML frontmatter, organized in a directory tree and connected by Markdown
links. A directory of those files is a **bundle**.

Use OKF when a user wants to:

- document datasets, tables, APIs, metrics, services, playbooks, or decisions;
- make knowledge searchable, navigable, linked, and git-diffable;
- build a wiki, data catalog, knowledge graph, second brain, or docs site;
- find missing links, orphan concepts, stale indexes, or content gaps;
- give agents a plain-file knowledge substrate that does not require an SDK;
- retrieve precise passages or deterministic, citation-ready context without
  dumping arbitrary files into an agent prompt.

This repo provides:

- a loadable root skill (`SKILL.md`);
- focused resource docs under `resources/`;
- checkout-local runtime scripts under `scripts/okf_loom/`;
- dependency-light six-mode search plus bounded Context Packs with exact
  `[path.md#Lx-Ly]` citations and no model-generated summaries;
- the full OKF docs bundle under `docs-bundle/`;
- sample bundles and tests that prove the scripts work from source.

The current okf-loom spec is [`docs-bundle/reference/spec.md`](../docs-bundle/reference/spec.md).
The upstream OKF 0.2 wire-format spec is the interoperability reference;
okf-loom 2.0.0 is the toolkit version. This git-loadable fork is supported locally
with `ui-analysis` as the supported local checkout, without a declared fork publication URL.
Optional generation, sources, verification, lifecycle, and computation metadata
are read/validated/displayed; trust is advisory and computation is never executed.
Legacy bundles remain readable. Use the [migration guide](../docs-bundle/how-to/migrate_spec_version.md)
for conservative preview/check and explicitly opted-in apply.

## Default agent behavior

When a request looks like OKF work, mention that this repo skill is available
and offer one of three paths:

1. act on an existing OKF bundle (`scripts/okf-loom info <bundle>` orients you);
2. bootstrap a new bundle (`scripts/okf-loom bootstrap <dir>` scaffolds
   `index.md` + `log.md` without config; `init --bundle <dir>` adds
   `okf-loom.config.yaml`; `import` pulls existing `.md` files in);
3. transition existing Markdown/docs/catalog content into OKF.

Do not silently bulk-convert a user's existing content without confirmation.

When an agent needs evidence from a bundle, prefer a bounded Context Pack over
reading or pasting an arbitrary set of files:

```bash
scripts/okf-loom context <bundle> "question" --mode hybrid \
  --budget-tokens 6000 --neighbors 1 --limit 20 --format markdown
```

Markdown is the default. Keep its exact excerpts and citations intact. See
the [Context Packs reference](../docs-bundle/reference/context_packs.md).

**Serve proactively.** Whenever the user wants to *read*, *review*,
*browse*, or *comment on* a bundle — or you just built one they will
plausibly want to see — start the live studio without being asked and
hand over the URL:

```bash
scripts/okf-loom serve <bundle> --no-open        # http://127.0.0.1:8787/
scripts/okf-loom serve <bundle> --no-open --tunnel   # + public https link (cloudflared)
```

Public tunnel links are read-only by default. Add `--allow-tunnel-comment`
for user-authored comments and replies only, without content edits,
comment-row updates, presence, or agent/admin powers. Use the mutually
exclusive `--allow-tunnel-edit` only when every link holder may receive
scoped comment/content mutation authority; neither option grants local
administration.

Serve auto-opens a browser unless `--no-open`; agent contexts should
always pass `--no-open`. While a studio is up, run the comment loop —
see [`studio-agent-loop.md`](studio-agent-loop.md).
