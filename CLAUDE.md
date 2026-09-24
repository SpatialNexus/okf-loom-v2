# CLAUDE.md — okf-loom

This repo is a loadable skill for working with Open Knowledge Format (OKF)
bundles. Orientation: read [`SKILL.md`](SKILL.md) first — especially its
**Default behaviours** section, which is the standing contract:

1. Author docs/knowledge as OKF concepts with fully-dressed frontmatter
   (see [`docs-bundle/demo/showcase.md`](docs-bundle/demo/showcase.md) for
   the worked example).
2. Validate after writing: `scripts/okf-loom validate <bundle>`; find gaps
   with `scripts/okf-loom discover <bundle>`.
3. Serve the live studio proactively whenever the user wants to see,
   review, or comment on docs: `scripts/okf-loom serve <bundle> --no-open`
   (add `--tunnel` for a public link; needs cloudflared).
4. While a studio is up, run the comment loop: `scripts/okf-loom wait` →
   `comment-claim` → mutators → `comment-resolve`
   (see [`resources/studio-agent-loop.md`](resources/studio-agent-loop.md)).

Ground rules: run everything via the checked-in `scripts/okf-loom` helper
(no package install exists); Python 3.11+; preserve unknown frontmatter
keys and hand-authored content; the binding contract on any ambiguity is
[`docs-bundle/reference/spec.md`](docs-bundle/reference/spec.md); the full
rule list is [`resources/gotchas.md`](resources/gotchas.md).
