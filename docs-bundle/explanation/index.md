# Explanation

This quadrant is the **understanding-oriented** surface of the okf-loom
docs bundle. Each concept below clarifies *why* a design choice was made
— the constraints, the alternatives considered, and the trade-offs
accepted. There are no steps here, and no neutral reference dumps; those
live in the [/how-to/](../how-to/index.md) and
[/reference/](../reference/index.md) quadrants.

Read these essays when you already know *what* okf-loom does and want
to understand the reasoning behind its shape.

# Concepts

| Concept | Clarifies |
|---|---|
| [What changed in okf-loom v2](v2_release_notes.md) | The honest one-page summary: original brief, timeline, old-vs-new comparison, feature inventory, demo links, and current proof status (what shipped and what is still pending) |
| [What OKF is for](what_is_okf.md) | Why knowledge lives in plain markdown files, why the graph matters, and what OKF deliberately is not |
| [okf-loom architecture](architecture.md) | One canonical model feeding three render targets, the module map, and why each module has its shape |
| [Why the live studio exists](live_studio_design.md) | The rationale behind the live studio: comment-driven directing, no review gate, all-on defaults, the archive track, the trust model |
| [Dependency-light philosophy](zero_dependencies.md) | Why okf-loom runs from a checkout with no primary install step, and how the PyYAML-preferred fallback works |
| [Why this bundle uses Diátaxis](diataxis.md) | The four content types, the four reader postures, and why mixing them confuses readers |

# Deep design rationale (imported from `docs/`)

Long-form discussion-oriented sources. These are the historical inputs
that shaped okf-loom; read them when the concise essay above leaves
you wanting the full reasoning.

| Concept | Clarifies |
|---|---|
| [Original requirements brief](requirements.md) | The user-stated goals + traceability matrix mapping each requirement to the code/docs that implemented it |
| [Best-of-breed research](research.md) | Historical research input — comparative analysis of Obsidian, Quartz v4, MkDocs, Docusaurus, semantic search libs, NER tooling, knowledge-graph stacks, Diátaxis |

# Conventions used in this quadrant

- These are **explanations, not instructions**. They discuss and compare;
  they do not walk you through steps.
- Each essay is **opinionated but fair**: it states okf-loom's choice
  *and* the alternative that was considered.
- ATX headings (`#`, `##`, `###`) in strict order break up long arguments.
- Tables compare options; blockquotes call out caveats and design maxims;
  fenced code clarifies a design when a fragment is worth a paragraph.
- All cross-links use the absolute bundle-relative form recommended by the
  [current toolkit spec](/reference/spec.md) and upstream OKF 0.2 §6.1,
  e.g. `[cli](/reference/cli.md)`.
- Every essay links out to a deep design document (now imported into
  [/reference/](../reference/architecture.md) and
  [/explanation/](research.md)) when the concise essay leaves you
  wanting the full reasoning.

# How an explanation differs from its neighbours

| Quadrant | Question it answers | Posture |
|---|---|---|
| [Tutorials](../tutorials/index.md) | *How do I learn this?* | Walks a beginner through reproduction. |
| [How-to](../how-to/index.md) | *How do I do this one task?* | Recipe for a known goal. |
| [Reference](../reference/index.md) | *What exactly does the machinery do?* | Neutral description. |
| **Explanation** (here) | *Why is it shaped this way?* | Discussion of choices and trade-offs. |

The framework behind this split is [Diátaxis](https://diataxis.fr/), which
this bundle adopts directly — see [Why this bundle uses
Diátaxis](diataxis.md).

# Source of truth

Current explanations describe **okf-loom 2.0.0 / OKF 0.2**. Research and release
records retain their historical scope rather than defining today's behavior.
Current authority:

1. The current spec and supporting design docs: [`/reference/spec.md`](/reference/spec.md),
   [`/reference/architecture.md`](/reference/architecture.md).
2. The checkout runtime and its source under `okf-loom/scripts/okf_loom/`.
3. Upstream OKF SPEC 0.2 as the base wire-format reference, linked from the current spec.
4. The [maintenance plan](/explanation/maintenance_plan.md) for current state and decisions.

When an essay and a deep doc disagree, the deep doc is the tie-breaker;
please flag the drift so the essay can be corrected.

# Where to go next

- New to okf-loom? Start with
  [/tutorials/install.md](../tutorials/install.md).
- Want the why behind the studio's comment model?
  Read [live_studio_design.md](live_studio_design.md).
- Wondering why there is no embedding backend?
  Read [zero_dependencies.md](zero_dependencies.md).
- Curious how this bundle is organised?
  Read [diataxis.md](diataxis.md).
