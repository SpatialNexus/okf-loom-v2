---
type: Demo
title: Rendering & Feature Showcase
description: One page that exercises everything the okf-loom viewer can render — diagrams, math, code, tables, governed keys, live features — so you can see it all in one place.
resource: /demo/showcase.md
tags: [demo, showcase, rendering]
timestamp: "2026-07-02T00:00:00Z"
aliases: [demo, kitchen sink, render demo]
entities:
  - label: Mermaid
    kind: library
  - label: KaTeX
    kind: library
  - label: highlight.js
    kind: library
relations:
  - type: references
    target: /reference/cli.md
  - type: explained_by
    target: /explanation/what_is_okf.md
provenance:
  - source: /demo/showcase.md
    note: Hand-authored demo content; safe to edit while testing the live studio.
citations:
  - id: diataxis
    text: "Diátaxis — a systematic approach to technical documentation. https://diataxis.fr"
---

# Rendering & Feature Showcase

For OKF 0.2 generation, source footnotes, trust/lifecycle, and computation fields,
see [metadata without execution](/demo/okf02_computation.md). This older rendering
example deliberately retains its authored legacy timestamp and Loom extensions;
these are not converted into inferred generation or verification records.

This page exists to be looked at. Everything okf-loom can render lives
below, and because you are (probably) viewing it in the **live studio**,
everything above this line — the type band, aliases subtitle, entity
chips, typed relations, provenance, and citations — came from the
frontmatter, not the body.

> **Try this while you read:** select any sentence on this page and a
> *Comment* button appears. The graph icon in the top bar shows how this
> page connects to the rest of the bundle.

## Text and structure

Standard Markdown works the way you expect: **bold**, *italic*,
`inline code`, and [internal links](/reference/cli.md) that get hover
previews — rest your pointer on that link. External links
[open in a new tab](https://example.com).

1. Ordered lists
2. With nesting
   - Unordered children
   - As deep as you need
3. And back out again

## Tables

Every table is interactive: **click a header to sort** (numbers, dates,
and currency sort numerically), **drag a header's right edge to resize**
the column, and on longer tables a toolbar adds a **row filter** and a
**Copy CSV** button. Column alignment follows the GFM separator row
(`:---`, `:---:`, `---:`).

| Feature | Where it renders | Rows | Added | Degrades to |
| :--- | :--- | ---: | :---: | :--- |
| Mermaid diagrams | This page, below | 2 | 2026-01-15 | Raw diagram source |
| KaTeX math | This page, below | 1 | 2026-02-01 | Raw LaTeX |
| Syntax highlighting | Every fenced block | 3 | 2025-11-20 | Plain monospace |
| Hover previews | Internal links | 12 | 2026-03-08 | Normal links |
| Live comments | Any selected text | 45 | 2026-06-30 | Read-only view |
| Sortable tables | Every table, incl. this one | 6 | 2026-07-03 | Plain static table |

## Task lists, strikethrough, footnotes

- [x] GFM task lists render as real checkboxes
- [x] ~~Old plan~~ struck through with `~~tildes~~`
- [ ] Bare URLs autolink: https://www.markdownguide.org/extended-syntax/
- [ ] Footnotes[^why] jump both ways

Hard line breaks work with two trailing spaces  
like this second line.

[^why]: Footnotes are written as `[^id]` inline plus a one-line `[^id]: text` definition — this one links back with the ↩ arrow.

## Diagrams (Mermaid)

Flowcharts render from a fenced ` ```mermaid ` block, loaded lazily from
a pinned CDN build only when a page needs it:

```mermaid
flowchart LR
    A[Author writes markdown] --> B{okf validate}
    B -->|clean| C[Live studio]
    B -->|findings| A
    C --> D[Comments from readers]
    D --> E[Agent applies edits]
    E --> A
```

Sequence diagrams too:

```mermaid
sequenceDiagram
    participant U as User
    participant S as Studio
    participant A as Agent
    U->>S: Comment on a concept
    S->>A: SSE comment event
    A->>S: Apply edit (attributed)
    S-->>U: Live patch, no reload
```

## Math (KaTeX)

A fenced ` ```math ` block renders display math:

```math
\operatorname{rank}(c) = \alpha \cdot \frac{\deg(c)}{\max_i \deg(i)} + (1 - \alpha) \cdot \sum_{t \in \text{tags}(c)} w_t
```

## Code with syntax highlighting

Python:

```python
from pathlib import Path

def concepts(bundle_root: Path) -> list[Path]:
    """Every concept is just a markdown file with frontmatter."""
    return sorted(
        p for p in bundle_root.rglob("*.md")
        if p.name not in {"index.md", "log.md"}
    )
```

JavaScript:

```javascript
// The graph consumes live SSE events — no reload needed.
window.okfLoomLive.on("graph", () => refreshGraphFromServer());
```

YAML (the shape of this very page's frontmatter):

```yaml
type: Demo
title: Rendering & Feature Showcase
tags: [demo, showcase, rendering]
timestamp: "2026-07-02T00:00:00Z"
relations:
  - type: references
    target: /reference/cli.md
```

## Blockquotes

> Documentation isn't done when there's nothing left to add.
> It's done when a reader can act without asking anyone.
>
> — every good docs team, eventually [diataxis]

## Governed keys on display

Scroll back to the top of this page and match what you see against the
frontmatter source (hit **Source** in the studio bar):

- **Aliases** render as the subtitle line under the title.
- **Entities** (Mermaid, KaTeX, highlight.js) render as chips with kind
  labels.
- **Relations** render as typed chips in the header *and* as labelled,
  colour-coded edges on the graph — open the graph view (top bar) and
  hover this node's edges.
- **Provenance** and **citations** get their own sections below the body.

## Live features to poke at

- **See what is new in v2:** the hands-on
  [Annotations v2 Studio walkthrough](annotations_v2_showcase.md)
  exercises anchor health, comments, resolution, re-anchor, six-mode
  search, and deterministic Context Packs with real controls.
- **Search everything** with the *Commands* button in the studio bar (or
  `Ctrl+K` / `⌘K`).
- **Watch it update live**: edit this file on disk and the page patches
  in place — scroll position, selection, and drafts survive.
- **The graph is alive too**: add a concept file and watch it fade into
  the canvas with a "Graph updated" chip.
- **Path tracing**: on the graph, click one node then shift-click
  another to light up the chain between them.
