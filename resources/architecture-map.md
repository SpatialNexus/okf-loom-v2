# Runtime architecture map

The runtime is not distributed as an installed package. It is source code under
`scripts/okf_loom/` and is invoked through the checked-in `scripts/okf-loom` helper.

| Area | Files |
|---|---|
| CLI dispatcher | `scripts/okf_loom/cli.py`, `__main__.py` |
| Data model | `model.py`, `parse.py`, `paths.py`, `roundtrip.py`, `ignore.py` |
| Validation | `validate.py` |
| OKF 0.2 metadata semantics / display adapter | `metadata.py`, `viewer/metadata.py` |
| Read-only migration planning / explicit apply | `upgrade.py` |
| Search/context retrieval | `retrieval.py`, `search.py` |
| Source-owned section destination proof | `navigation.py`, `RetrievalService.verified_section_fragment` |
| Index/discovery | `index.py`, `discover.py`, `plan.py`, `update.py` |
| Bundle config/capabilities | `config.py`, `extensions.py` |
| Annotations v2 capture/resolution | `annotations.py` |
| Markdown page identities / source-aware navigation / portable review metadata | `documents.py`, `navigation.py`, `review_links.py` |
| Canonical Markdown block IR/projection grammar | `markdown_blocks.py` |
| Rendering/server/studio | `render.py`, `export_ownership.py`, `server.py`, `studio.py`, `watch.py` |
| Session authority/directive lifecycle | `session_authority.py`, `directive_store.py`, `presence.py` |
| Experimental proposal backend/transports | `proposal_review/`, `proposal_snapshot.py`, `proposal_runtime_identity.py` |
| Capture provenance | `capture_provenance.py` |
| Viewer and portable retrieval assets | `viewer/templates/`, `viewer/static/portable-retrieval.js`, `viewer/static/static-search.js`, `viewer/static/single-search.js`, `viewer/static/capture-readiness.js`, `viewer/OVERRIDES.md` |
| Archive/benchmark/proof helpers | `scripts/build_skill_archive.py`, `scripts/benchmark_gold_standard.py`, `scripts/capture_annotations_v2.py`, `scripts/capture_viewer_proof.py`, `scripts/capture_signal_controls.py`, `scripts/capture_ui_overhaul.py`, `scripts/lint-js.sh` |

`retrieval.py` derives section passages from the canonical in-memory bundle,
shares six-mode ranking between search and Context Packs, and emits the
versioned content-addressed corpus used by portable builds. These indexes and
packs are read-only derived artifacts, not a second source of truth, database,
or model-backed service.

`metadata.py` owns generation fallback, aware timestamps, source windows, trust,
and lifecycle semantics. `viewer/metadata.py` escapes that read-only projection
and joins source footnotes by exact id. Computation fields are never executed.
`upgrade.py` preserves legacy evidence through conservative additive per-file
writes with declaration last; see the [migration guide](../docs-bundle/how-to/migrate_spec_version.md).

`markdown_blocks.py` owns the shared block grammar and source projection IR used
by annotation capture, Markdown rendering, and projection generation. The
renderer marks owned nodes and the projection finalizer verifies them fail
closed; `annotations.py` owns anchor capture and resolution. See the canonical
[specification](../docs-bundle/reference/spec.md) for normative protocol details.

Authoritative documentation:

- [`docs-bundle/reference/architecture.md`](../docs-bundle/reference/architecture.md)
- [`docs-bundle/reference/cli.md`](../docs-bundle/reference/cli.md)
- [`docs-bundle/reference/http_routes.md`](../docs-bundle/reference/http_routes.md)
- [`docs-bundle/reference/context_packs.md`](../docs-bundle/reference/context_packs.md)
- [`docs-bundle/reference/spec.md`](../docs-bundle/reference/spec.md)
- [`docs-bundle/explanation/maintenance_plan.md`](../docs-bundle/explanation/maintenance_plan.md) — current maintenance priorities

The architecture reference's session/proposal ownership section routes to
current lifecycle and test seams.

Tests in `tests/` are the executable examples for checkout-local behavior.

### Atomic-write permissions

`io_utils.py` keeps temporary files at 0600 until their content is flushed,
then preserves an existing target's ownership and `st_mode & 0o7777` before
replacement. New ordinary content uses `0666 & ~umask`. Linux reads the live
mask from `/proc/self/status`; if unavailable, an isolated Python child reads
its inherited mask without ever changing the multithreaded parent's umask.
Ownership that cannot be retained fails the write rather than silently changing
owners. The durable helper still fsyncs the file before replace and the parent
directory afterward.

Private writers explicitly pass `mode=0o600`, overriding even a wider existing
file: Studio rotation counters, idempotency/dedup maps, event sequence state,
comment/reanchor WALs, receipts and compaction filters, denial audits, undo
snapshots/group manifests, and index/update rollback of session sidecars. Local
derived-index caches and generated state-directory ignore files do likewise.
Studio CSRF tokens/server state, descriptor-relative comment receipts,
DirectiveStore and PresenceStore retain their independent 0600 creation paths;
proposal authority I/O additionally rejects existing non-0600 files. None of
these private paths inherit ordinary content's permission-preservation policy.

### Internal section-navigation proof

`RetrievalService.verified_section_fragment(concept_id, passage_id, revision=...)`
is a read-only internal adapter over one bundle snapshot. It returns a decoded
DOM heading id only for a current, canonical, same-concept passage whose source
coordinates align with a uniquely identified heading emitted by the shipped
renderer. It does not slugify heading text. Stale revisions, preambles,
renderer/retrieval grammar differences, ambiguous ids, and source/body offset
differences return `None`. Duplicate titles can map when their actual rendered
ids remain unique. Passage text, citations, ranking, and strict schema-v1
passage/corpus records are unchanged; this method adds no serialized metadata.

Search adds **Open matching section** separately from ordinary Open only when
the evidence passage also has a textual query hit. `build_section_navigation`
reuses one renderer pass per concept to emit bounded optional presentation
metadata, separate from the unchanged strict corpus. `render.py` owns export
bootstraps; the live HTML adapter in `server.py` does not change search JSON.
Portable parsing binds the map to the admitted corpus revision, while each
destination binds its canonical concept and `concept_content_rev` of normalized
source independently. Neither digest is authorization or proof of disk bytes.

`document-outline.js` admits a unique actual heading within the displayed
concept, with its expected renderer label, before publishing the hash and
focusing below measured chrome. The existing selected-document owner in
`graph.js` does the same for single-file reading; `single-search.js` owns the
search-panel history snapshot. One Back restores search inputs and results;
ordinary source hashes, outline controls, and root Open retain their owners.
Missing, stale, malformed, duplicate, or plugin-modified destinations fall back
to root reading. See spec §9 for the optional transport and absence contract.

### Capture readiness

The capture-readiness runtime owns semantic target/revision/layout readiness.
Capture consumers apply one 30-second monotonic budget across navigation,
prepare, readiness, paint, and screenshot; failed recapture removes only its
unique invocation-owned temporary and preserves any prior valid destination.
Managed late workers and cross-process destination sequencing prevent stale
publication. GIF proof assembly uses private frame staging, verifies, and
atomically replaces through the same envelope; phase helpers preserve one-shot
and recurring outer timers. The standalone capture and benchmark scripts are
maintainer proof tooling, not subcommands of the primary CLI.
Synchronous Playwright remains main-thread owned, and failed server context
entry closes any socket, thread, or process it already started.
Ignored one-shot/periodic alarm schedules retain `SIG_IGN` semantics while the
capture operation's own deadline remains effective.

## Selected-revision UI-overhaul capture

```bash
python scripts/capture_ui_overhaul.py --revision <full-commit-oid> --out <external-dir>
```

This opt-in selects **both the capture driver and product** from one local
commit. Without `--revision`, clean-HEAD admission and schema-3 provenance are
unchanged (including dirty/shallow rejection). `--manifest-only` remains the
current driver's scenario listing and cannot be combined with `--revision`.
This is UI-overhaul tooling only: annotation capture, its source/evidence commit
rules, and annotation manifests are unchanged.

The launcher accepts a full lowercase SHA-1/SHA-256 commit OID for the repository's
object format, not a branch, abbreviated OID, tree, blob, or tag. It verifies the
commit and every required tree/blob by object identity and materializes the
complete tree through `ImmutableRuntimeWorkspace`. Dirty, detached and shallow
callers are allowed when every selected object is already local; shallow parent
history is not a capture input. There is no checkout/reset/stash/worktree/fetch or
automatic object hydration. Grafts, replace refs (including packed refs),
alternates, promisor/partial-clone metadata, unsafe shallow metadata, unsafe tree
paths and non-regular modes are rejected. Empty tree directories are unsupported.
`--bundle` is repository-relative (or an absolute path beneath the source root)
and is admitted against the selected snapshot, not caller file existence.

### Driver protocol and ownership

The selected `scripts/capture_ui_overhaul.py` must declare the literal
`SELECTED_CAPTURE_PROTOCOL = "okf-ui-selected-v1"` and implement
`selected_capture_v1(request)` with one required argument. Missing, malformed or
unsupported historical drivers fail explicitly; there is **no current-driver
fallback**. A separate Python process imports only that selected repository's
script path. Its private JSON request has exactly `protocol`, `runtime_root`,
`source_root`, `stage`, `bundle`, `chrome`, `provenance`, `inventory`, and
`inventory_digest`. There is no public internal-worker CLI flag.

The selected driver verifies its identity/inventory, executes its own scenario
matrix and existing readiness/deadline/page-revision checks, writes the private
stage, and returns zero only on successful validation. It does not publish.
The launcher rechecks inventory and returned provenance, then uses the existing
destination lock, publication verifier, two-rename replacement and recovery
path. It does not recheck symbolic HEAD after materialization. Failures before
publication leave prior output unchanged; interruption/publication errors can
retain recovery or a complete published generation. Inspect the destination
and sibling `.<destination-name>.capture-authority` before retrying; do not
blindly delete recovery. Driver output is suppressed rather than copying
potentially sensitive diagnostics into evidence.

Ledger capture observations use `theme_state.choice` for the effective
appearance preference and `resolved_theme` / `resolved_mode` for the painted
palette and scheme. Retired contrast/border modifier fields are not emitted.
The existing scenario and manifest `theme_family` / `theme_mode` columns are
compatibility names for choice and scheme/System, not active family axes.

Schema **4** identifies `source_provenance.kind: selected-commit`, with protocol,
selected `commit`/`tree`/`object_format`, driver `path`/Git blob `oid`/`sha256`, and
the verified inventory digest. Complete `input_inventory` and equal before/after
digests remain in the manifest. Selected artifacts carry this source variant,
not `clean`, worktree status or worktree digest. Manifest `source_revisions` and
artifact expected/observed/source/bundle revisions are **rendered content IDs**,
not Git commit IDs; schema-3 top-level revision aliases are absent in schema 4.
Consumers must explicitly support the new variant rather than treating private
snapshot validity as `clean: true`. No dirty filenames, diffs or ambient
environment are serialized as provenance.

This executes **trusted local repository/revision code**, not sandboxed code.
Git identity proves which code ran, not that it is safe or that its assertions
are honest. Python, Playwright, browser binaries and display/browser environment
remain operator-owned host dependencies; no dependency is installed or fetched
by this opt-in. Existing headed-zoom admission remains required. Unit fixtures
prove dispatch and failure fences, not real-browser compatibility or human
accessibility acceptance.

Live served-byte provenance admits generated `ledger-fonts.css` only after the
immutable runtime's vendor loader verifies its locked WOFF2/OFL inputs and
derived size/SHA-256 pin. The loader runs from that snapshot, never the caller's
checkout. Raw inputs remain in the inventory; sealed static outputs (including
derived KaTeX CSS) remain bound to their separate build inventory.
