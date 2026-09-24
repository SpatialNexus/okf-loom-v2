# Graph lenses — evidence-based view modes

The graph presets (Overview / Hubs / Bridges / Relations / Types /
neighborhood exploration over a raw layout dropdown) looked configurable but revealed little:
color restated the type schema, "Hubs" ranked by raw degree, and the layout
picker was an algorithm buffet. This document records the research that
replaced them and the decisions it grounds.

## Research inputs

Two commissioned digests (2026-07-02), summarised here:

**A. Product survey** — Obsidian (+ Juggl/Graph-Analysis/ExcaliBrain), Neo4j
Bloom, Gephi, Kumu, InfraNodus, Linkurious, yFiles, TheBrain, Roam/Logseq.

**B. Academic digest** — key sources: Ghoniem/Fekete/Castagliola 2004-05
(node-link vs matrix); Purchase 1997 + Huang 2008 (edge crossings);
Venturini/Jacomy/Jensen 2021 (proximity as structural proxy); Archambault &
Purchase 2012-13 (layout stability / mental map); Pohl/Schmitt/Diehl 2009 +
Burch 2011 (layered vs force vs radial); McGrath/Blythe/Krackhardt 1997
(position dominates perceived importance); Saket et al. 2014 (group
encodings help cluster tasks, cost ~25% on topology tasks);
Shneiderman 1996 + van Ham & Perer 2009 (overview-first vs search-first);
Moscovich et al. 2009 (Bring & Go / focus techniques); Lee et al. 2006
(graph task taxonomy); Archambault/Purchase/Pinaud 2011 (small multiples
beat animation for temporal).

## The findings that drove the design

1. **Color must carry computed structure, not the schema.** Three
   independent tools (Gephi's canonical workflow, InfraNodus's only view,
   Bloom's GDS loop) converge on the same composite: force layout +
   community color + centrality size. Venturini et al. explain why: layout
   proximity is only legible when triangulated with the other channels.
   Type-as-color is the documented anti-pattern (it restates what users
   know) — type moved to the node **icon**, color now carries **detected
   communities** (deterministic Louvain modularity).
2. **Degree-hubs are noise; encode the metric explicitly.** Kumu's own docs
   warn degree finds loud nodes, not important ones. McGrath 1997 shows
   users read *position* as importance unless a metric is explicitly
   encoded. So: Map sizes by **PageRank**, Bridges sizes and colors by
   **betweenness** — explicit channels, real algorithms (Cytoscape.js
   built-ins), never just layout position.
3. **Group coloring costs topology accuracy → toggle.** Saket et al. 2014
   measured ~25% accuracy loss on plain topology tasks under group
   encodings. `Advanced → Colour by theme` turns community color off
   (falling back to the type palette) exactly as the evidence mandates.
4. **Deterministic, stable positions.** "Nodes change on every load" is the
   top Obsidian complaint; Archambault & Purchase show stable positions
   matter when re-finding nodes. Boot seeds positions deterministically
   (sorted ids on a circle) and every layout runs `randomize:false`; the
   community detection itself is order-deterministic.
5. **The one non-force layout with proven demand is layered/flow.**
   yFiles' flagship, Linkurious's second layout. Evidence is task-specific
   (layered beats radial for hierarchy reading; force can beat layered for
   path-finding — Pohl 2009), so Flow is a lens for *direction reading*,
   while path-finding stays with shift-click tracing in any lens.
6. **Time is a wasted, loved channel.** Obsidian's timelapse is the feature
   users make videos of; Bloom/Linkurious ship time slicers. Recent encodes
   staleness as a static color ramp (per Archambault 2011, static/small-
   multiple beats animation for analysis).
7. **Ranked lists beside the picture.** Graph-Analysis renders insights as
   tables; InfraNodus pairs every graph with a ranked panel; Kumu surfaces
   "top bridgers" as *answers*. Every lens ships a clickable ranked summary
   with a one-line **verdict** (InfraNodus's judgment pattern: "6 themes ·
   2 not yet linked · areas X and Y share no links").
8. **Local neighborhood exploration is the consistently-praised mode**
   (Obsidian local graph, Kumu Focus, TheBrain's whole existence; Moscovich
   2009). Kept as **Explore neighborhood**, with dim-don't-hide (Kumu
   Showcase's figure/ground insight).
9. **Question-named modes.** Every successful tool names views by the
   structure revealed, never the algorithm. Each lens carries its question
   in the bar ("Which concepts hold the areas together?") and as its chip
   tooltip.

## The lens set

| Lens | Question | Layout | Encoding | Summary panel |
|---|---|---|---|---|
| **Map** (default) | How does this knowledge fit together? | fCoSE, community-grouped | color = community, size = PageRank, icon = type | themes digest + orphans + structural-gap verdict |
| **Themes** | What are the main topic areas — and which barely touch? | fCoSE, strong community pull | color = community | ranked themes (size, anchor concept), gap verdict |
| **Flow** | What feeds into what? | dagre LR (fallback: directed breadthfirst) | color = community, typed-edge colors + labels | sources/sinks counts, top feeders |
| **Bridges** | Which concepts hold the areas together? | fCoSE, spread | color+size = betweenness, glow on top bridgers | ranked bridgers with % |
| **Recent** | What is fresh, what is going stale? | fCoSE | color = recency ramp (teal→amber→slate; undated neutral) | stalest list, stale/undated verdict |
| **Explore neighborhood** | What surrounds the selected concept? | current layout | neighborhood keeps full opacity, rest dims | depth hint |

Removed: the layout dropdown (each lens owns its layout — pros ship three
semantic layouts, not twelve algorithm names), the Types preset (type is
always visible as the icon; the legend chips filter types in every lens),
the degree-Hubs preset (PageRank sizing in Map supersedes it), and the
heuristic "signal weighting" control cluster.

## Implementation notes

- Louvain (single-level iterated, deterministic order, ties to smallest
  community, renumbered by size) replaced label propagation after the
  densely-linked docs bundle collapsed to one community under label prop.
- All analytics are lazy + cached per graph structure
  (`invalidateLensCache()` on live SSE deltas), computed client-side:
  PageRank and betweenness via Cytoscape.js built-ins, Louvain ≈70 lines.
- dagre + cytoscape-dagre ship pinned with SRI beside the fcose chain.
- Boot no longer auto-selects a node: the first thing the right pane shows
  is the active lens's ranked answers.

## Future candidates (evidence-noted, not built)

- Matrix view for dense bundles (Ghoniem: matrices beat node-link past
  ~20 nodes/high density except for path-finding).
- Time scrubber/slicer (Bloom-style) over the Recent lens.
- "Search first, expand on demand" empty-canvas mode for >200-node bundles
  (van Ham & Perer DOI subgraphs).
- Link prediction / co-citation suggestions as a ranked list
  (Graph-Analysis's Adamic-Adar pattern) feeding `discover`.
