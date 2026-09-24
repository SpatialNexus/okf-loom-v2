---
type: Explanation
title: What OKF is for
description: Why the Open Knowledge Format exists — knowledge as plain, diffable, agent-parseable markdown files linked into a graph — and what it deliberately is not.
resource: /explanation/what_is_okf.md
tags: [okf, format, rationale]
timestamp: "2026-06-29T00:00:00Z"
---

# What OKF is for

This essay explains *why* the Open Knowledge Format (OKF) exists, what
problem it picks, and which problems it deliberately leaves to other
tools. It is not a reference for the format's keys — that is
[frontmatter.md](/reference/frontmatter.md) — nor a tutorial for writing
your first file — that is
[Author your first bundle](/tutorials/first_bundle.md). It is the
reasoning behind both.

The current okf-loom contract lives in the
[current specification](/reference/spec.md).
The upstream OKF SPEC 0.2 is the base wire-format reference
okf-loom implements.

# The problem OKF picks

Most knowledge that surrounds code and data — the metadata, the context,
the schemas, the relationships, the playbooks, the decisions — does not
have a natural home. It ends up scattered across a wiki, a data catalog,
a set of design docs, some tickets, some chat threads, and a great deal
of tribal memory that lives only in people's heads.

Each of those homes optimises for one audience. A data catalog is
optimised for a governance team and a query engine. A wiki is optimised
for a human clicking through pages. A design doc is optimised for a
reader absorbing one long argument. None of them is optimised for *all*
of the audiences that actually need the same knowledge today: a human
browsing it, a reviewer diffing it, and an agent reading and editing it
in collaboration with a human.

OKF picks the problem of representing that knowledge in a form that
serves all three audiences at once, with the smallest possible
mechanism. Its answer is deliberately unsexy: **a directory tree of
plain markdown files with YAML frontmatter, linked into a graph by
ordinary markdown links.**

# Knowledge as files

The founding bet is that *files* are the right unit of knowledge.

A file is readable by a human without any tooling — `cat` works. It is
parseable by an agent without an SDK — the frontmatter is YAML and the
body is markdown, both of which a language model can read and write
directly. And it is diffable in git, which means knowledge becomes a
normal software-engineering artefact: versioned, reviewed, branched,
blamed, and reverted with the same commands you already use for code.

Consider a single concept file:

```markdown
---
type: BigQuery Table
title: Customer Orders
description: One row per completed customer order across all channels.
resource: https://example.com/path/to/asset
tags: [sales, orders, revenue]
---

# Schema

| Column        | Type    | Description |
|---------------|---------|-------------|
| `order_id`    | STRING  | Globally unique order identifier. |
| `customer_id` | STRING  | FK to [customers](/tables/customers.md). |
```

Every one of those audiences reads the same artefact. A human opens it
in their editor. A reviewer runs `git diff` and sees exactly which row
changed. An agent reads the YAML and the table, reasons about them, and
writes the next link — without a database, without a query layer,
without a serialisation format it has to learn.

> The SPEC is intentionally minimal: a bundle is conformant if every
> non-reserved file has parseable frontmatter with a non-empty `type`,
> and the reserved files follow their structure. Everything else is
> soft guidance. The format optimises for *never rejecting knowledge*,
> not for *enforcing a schema*.

This is also why the only hard-required key is `type`. A string that
names the *kind of thing* (`BigQuery Table`, `Playbook`, `API Endpoint`,
`Tutorial`) is enough for a consumer to route the concept without
demanding a registry the producer has to satisfy. Recommended keys
(`title`, `description`, `resource`, `tags`) are
soft guidance (strict producer checks may promote findings); producers may add any further keys, and
consumers must preserve them round-trip.

OKF 0.2 adds optional generation, verification, sources, lifecycle, and computation
metadata. These help readers assess origin and currency without making trust an
authorization mechanism. Loom reads, validates, and displays computation contracts;
it never executes them. Legacy timestamps remain a fallback only when generation
metadata is absent; imports do not gain invented authors or verification.

# Three audiences, one artefact

The format is shaped by serving three readers from the same file.

| Audience | What they need | How a plain markdown file serves them |
|---|---|---|
| A human browsing | Readable text, navigation, search | Markdown renders to HTML; links are navigable. |
| A reviewer / auditor | Diffable, blameable history | One file per concept; `git diff` shows the change. |
| An agent (LLM) | Parseable, editable, no SDK | YAML + markdown are directly readable and writable. |

The third row is the one that most prior formats ignored. A wiki stores
its pages in a database; a data catalog stores its entries behind an
API; a design-doc system renders from a proprietary source. In each
case an agent that wants to *edit* the knowledge has to learn a
toolchain, hold an API token, or round-trip through an exporter. OKF
makes the agent a first-class author: it reads the file, reasons, and
writes the file back, exactly as a human would in their editor.

This is not a theoretical benefit. okf-loom's whole live studio is
built on it — the agent authors `.md` files through ordinary mutator
commands, and a watcher pushes the change to every open surface. See
[Why the live studio exists](live_studio_design.md) for that model.

# A graph, not just a tree

A directory tree alone is not enough, because knowledge is not
hierarchical. `tables/orders` relates to `tables/customers` (a foreign
key), to `services/checkout` (the writer), to `playbooks/refund_flow`
(a consumer), and to `glossary/revenue` (a definition). None of those
relationships fits neatly under one parent directory.

OKF resolves this the way the wider notes-and-knowledge community
resolved it: ordinary markdown links between files, turning the tree
into a graph. A link in the body of one concept to another is not just
decoration for a human — it is a first-class *graph edge* that
okf-loom extracts, indexes, and renders.

```markdown
| `customer_id` | STRING | FK to [customers](/tables/customers.md). |
```

That `[customers](/tables/customers.md)` is, simultaneously: a
clickable link for a human; a relationship the discovery pass can
reason about; an edge in the force-directed graph view; and a
backlink on the target concept's page. One syntactic choice, four
consumers served.

The link extractor recognises both forms the SPEC permits — absolute
bundle-relative (`/tables/customers.md`, recommended and stable when
files move within a subdirectory) and relative (`./customers.md`, for
tightly-coupled siblings) — plus Obsidian-style wikilinks
(`[[customers]]`) as a soft input. The full link model is documented in
[links.md](/reference/links.md).

# Vendor-neutral and lock-in-free

A bundle is a directory. That is the entire deployment story. There is
no server you must run, no account you must hold, no proprietary cache
directory, and no schema registry your bundle is coupled to. You can
`tar` a bundle, email it, drop it in a repository, open it from
`file://`, or mount several of them together.

The okf-loom runtime that consumes a bundle is also vendor-neutral. It is
**harness-agnostic**: the same checkout runtime runs as a standalone CLI, as a
Python library, or as a long-running viewer embedded inside any agent
harness (opencode, Claude Code, Codex, a custom pipeline). It does not
assume which agent framework consumes it, and it does not run an LLM
itself — the server emits events and runs mechanical operations; the
agent does the thinking externally and writes back through ordinary
mutator calls. See [Embed the viewer in an agent
harness](/how-to/embed_in_harness.md) and the deep
[`/reference/embedding_guide.md`](/reference/embedding_guide.md) for
the wiring shapes.

Lock-in-freedom has a concrete consequence: **because a bundle is just
files, the worst case is that you stop using okf-loom and read the
files with something else.** The format is the contract, not the tool.
`cat` is a fully supported OKF viewer.

# How OKF compares to the neighbours

okf-loom's [`/explanation/research.md`](/explanation/research.md) surveys
the best-of-breed tools OKF learned from. The short version, framed by
the problem each one solves:

| Neighbour | What it optimises | Why OKF does not collapse into it |
|---|---|---|
| **Obsidian / Logseq** (local markdown vaults) | A rich personal reading + authoring UX | Proprietary binary cache, desktop GUI, wikilink-first syntax. OKF keeps standard markdown links as canonical and is a format + a harness-agnostic toolkit, not an app. |
| **Quartz** (publish an Obsidian vault) | A static site from a folder of interlinked markdown | The closest analog. OKF borrows Quartz's "one content index feeds many features" lesson, but adds SPEC-conformance validation, agent-authorable mutators, and a live collaborative studio rather than only a publish step. |
| **MkDocs Material** (docs site) | A polished, server-rendered, searchable docs site | Single-page-at-a-time, no client graph, linear `nav`. OKF is graph-first; MkDocs-style server-rendered HTML is one of okf-loom's *build targets*, not its identity. |
| **A data catalog** (DataHub, Amundsen, OpenMetadata) | Governed metadata + a query layer for a data org | Backed by a database, schema-registry-coupled, optimised for governance. OKF has no schema registry, no database, and optimises for diffable, agent-editable files. A catalog can *export* to OKF; OKF does not try to be one. |
| **A wiki / Notion** | Capture and browse unstructured team knowledge | Hosted, block-graph-in-a-database, not portable, not diffable, not git-friendly. OKF is the opposite on every one of those axes. |
| **A design doc** | One long argument a reader absorbs | A single artefact with no machine-parseable structure or cross-links. OKF *is* a good home for design docs (each doc is a concept), but adds frontmatter, links, and graph relationships. |

The pattern: OKF does not compete with these tools on the axis each one
optimises. It competes on the axis none of them optimise — *one
artefact, three audiences, no toolchain lock-in* — and it is happy to
sit alongside them. A bundle can be generated *from* a catalog and
*rendered into* a docs site.

# What OKF deliberately is NOT

Naming the boundaries is as important as naming the goals.

## It is not a content management system

OKF has no account system, no permissions model, no workflow state
machine, and no review queue. The live studio is single-user local-admin
by design: one human owns the bundle and the agent that works for them,
and the studio never gates that human. (Constraints a user wants to
impose on *their own* agent are an opt-in, never a default — see the
trust model discussion in [live_studio_design.md](live_studio_design.md).)

## It is not a database

There is no query language, no indexing engine you must run, and no
storage layer that is not the filesystem. okf-loom computes a graph
and a content index lazily, in memory, memoised on the `Bundle` object,
and thrown away when the bundle is garbage-collected. If you want
rich ad-hoc queries over history you project the append-only change
feed into your own SQLite in your own module; okf-loom does not
impose that complexity on core. (See [`/reference/architecture.md`](/reference/architecture.md)
§ "Performance characteristics".)

## It is not a render-only format

The `.md` files are the source of truth, not a render target. Unlike a
system where you author in a rich editor and export to markdown, with
OKF you author *in* markdown, and the renders (live HTTP server,
single-file HTML, multi-file static site, SPA) are projections of the
same canonical model. This is what makes the agent a first-class
author: it edits the source, not a rendered shadow.

It is also not the case that the format *requires* okf-loom to be
useful. okf-loom is the production layer — it validates, searches,
discovers, updates, and renders — but the format stands on its own.
Anything that reads markdown and YAML can read an OKF bundle.

# Why "minimal" is the feature

The temptation for any knowledge format is to grow: to add a type
registry, a relation vocabulary, an indexing schema, a query language.
OKF resists all of those at the spec level and instead offers them as
*optional, recognised-if-present* extensions behind a capability
registry. Typed relations, entities, aliases, citations, and the Diátaxis
content-type vocabulary are all available to a bundle that wants them —
and invisible to a bundle that does not. See
[capabilities.md](/reference/capabilities.md) for the catalogue.

The payoff is that a producer never has to satisfy the format to
contribute knowledge, and a consumer never has to understand the whole
format to read some of it. The floor is `type: something`; everything
above it is opt-in. That is the whole point: a format that *gets out of
the way* of capturing knowledge, so the knowledge actually gets
captured.

# See also

* [Author your first bundle](/tutorials/first_bundle.md) — turn an empty
  directory into a small valid bundle.
* [Frontmatter contract](/reference/frontmatter.md) — the SPEC §4.1 keys,
  required versus recommended, and the YAML gotchas.
* [Link forms](/reference/links.md) — absolute versus relative links,
  anchors, wikilinks, and broken-link tolerance.
* [Why this bundle uses Diátaxis](diataxis.md) — how the four content
  types map onto OKF's `type:` key.
* [Current okf-loom specification](/reference/spec.md) — the single current
  build-from-docs contract for this repository.
* [Upstream OKF SPEC](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) —
  the base wire-format reference okf-loom implements.
* [`/explanation/research.md`](/explanation/research.md) — the comparative
  survey of Obsidian, Quartz, MkDocs, Notion, and the rest that shaped
  these choices.

Return to [Explanation](/explanation/index.md).
