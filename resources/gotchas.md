# OKF hard rules and gotchas

## Hard rules

1. OKF 0.2 only always requires `type` in concept frontmatter. Optional families
   remain soft guidance; `generated.by` is required within a generation mapping.
2. Preserve unknown frontmatter keys round-trip.
3. Consumers tolerate broken links; authoring mutators fail closed unless a
   forward reference is explicitly allowed.
4. Absolute bundle-relative and relative Markdown links are both valid. Prefer
   absolute bundle-relative links when authoring.
5. `index.md` and `log.md` are reserved filenames, not concept documents.
6. Only the bundle-root `index.md` may carry frontmatter.
7. Auto-update must not destroy hand-curated content; index regeneration is
   marker-safe and log updates are append-only.
8. All okf-loom writes go through tmp + rename atomic writes.
9. Use `scripts/okf-loom`; do not assume package installation.
10. Never commit `<bundle>/.okf-loom/session/` — it holds ephemeral live-feed
    state and the per-session `.token` secret. The runtime attempts to write
    self-ignoring `.gitignore` files containing `*` in session and derived-index
    directories. Keep them, but verify Git status before publishing: writing an
    ignore file can fail, ignores do not untrack existing files, and forced adds
    can bypass them. These defaults are not a confidentiality guarantee.
11. `serve` binds 127.0.0.1:8787 by default and auto-opens a browser
    unless `--no-open`. Going public is an explicit choice:
    `--tunnel` (cloudflared quick tunnel, read-open to link holders) or
    `--public --public-ack` (raw network bind). Forgot `--tunnel`? Do NOT
    kill and restart serve — `scripts/okf-loom tunnel <bundle>` attaches
    one to the running session (`--stop` detaches).

## Common traps

- Concept `generated: {by, at}` is provenance, not root-index ownership. Only
  literal root `generated: true` grants whole-body regeneration without markers.
- Use real offset-bearing datetimes. Legacy `timestamp` is a fallback only when
  `generated` is absent; never fabricate actors or verification to silence warnings.
- Absent status is stable; stale means on/after expiry. Neither lifecycle nor
  advisory trust tiers confer authorization. Computation metadata never executes.
- Keep source footnote ids stable. Source usage is a signal, not a score; per-source
  windows override shared windows. Loom citations and Context Pack line citations
  are separate contracts, not replacements for `sources`.

- Bundle scanning prunes hidden dirs, `node_modules`, nested git clones, and
  `.gitignore`-d paths by default (spec §5). This is a visibility default, not
  proof that a workspace root is safe to share. Select a deliberate bundle and
  review its visible content. A gitignored `.md` normally will not load, but can be
  added back with `bundle.include` in `okf-loom.config.yaml` (beats every
  exclusion, even nested-repo pruning — e.g. `include: [vendor-repo/]`);
  tune the rest with `bundle.exclude` / `bundle.respect_gitignore`.
- Images/media must be bundle-local files: the studio serves (and static
  builds copy) allowlisted media (`.png .jpg .jpeg .gif .webp .avif .bmp
  .ico .svg .mp4 .webm .pdf`) under the same §5 visibility rules as
  concepts — a gitignored/pruned asset will 404 in the studio too. Remote
  hot-linked images are CSP-blocked and `data:` URIs are sanitized by
  design. `validate` flags missing image targets (`asset.missing`).
- A default (`replace`) static/SPA build wipes its output directory. Use
  `--output-policy managed` to refuse unknown or edited files instead. A default
  build into a managed directory drops `.okf-export.json`, and `--adopt-output`
  succeeds only on an exact byte match (a passed `stale_after` can break it).
- YAML parses unquoted dates/version-like values. Quote timestamps and versions.
- `tags` should be a YAML list, not a comma-separated scalar.
- Markdown links inside code blocks are not graph edges.
- Search/context bounds and scalar filters are strict. Duplicate, unsupported,
  or mode-inapplicable parameters fail closed; do not expect a silent lexical
  fallback. Portable static no-JS pages cannot run arbitrary retrieval.
- `search --format` accepts only `text` or `json`; `md` and `dot` are not
  search formats. Context Pack formats are `json` and `markdown`.
- Do not use `type` as a source-system or per-dataset taxonomy. Use tags and
  custom keys for cross-cutting classifications.
- Do not add a database or sidecar schema for what OKF already represents in
  plain Markdown.
- Do not reintroduce package-publishing as the primary distribution story; this
  git repo skill layout is the artifact.
