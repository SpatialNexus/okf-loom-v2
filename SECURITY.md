# Security Policy

okf-loom 2.0.0 is an Apache-2.0 open-source repository skill for local OKF 0.2 bundle authoring, serving, and viewing.

## Reporting a vulnerability

Please report suspected security issues privately to the repository maintainers before opening a public issue.
Include the affected commit, commands used, expected impact, and whether the issue requires a malicious bundle, a network-exposed studio, or local filesystem access.

## Supported scope

This fork's supported local checkout is `ui-analysis`. No fork publication
or private-reporting URL is declared; use an established private maintainer channel.
The primary runtime path is repo-local execution with `scripts/okf-loom`; PyPI or package publishing is not part of the supported release process.

## Live studio exposure boundary

`scripts/okf-loom serve` binds to `127.0.0.1` by default.
Using `--public`, a non-loopback `--host`, or `--tunnel` changes the exposure boundary.

- Anyone with a reachable studio URL can read public GET surfaces for the served bundle.
- Local/admin mutating POST routes require the per-session `X-OKF-Token` and Origin/Host allow-list checks.
- `--tunnel` points Cloudflare at a dedicated, generation-bound public ingress listener; it does not widen the local/admin allow-list.
- Public tunnel access is read-only by default. The explicit `--allow-tunnel-edit` opt-in gives every URL holder a separate scoped token for comment/content mutations only, never the local admin token.
- Use `--no-edit` to disable bundle mutations across the studio when sharing a URL for review.
- Treat `<bundle>/.okf-loom/session/.token` as secret; session state is intended to stay out of commits.

Active viewer override/plugin code is disabled unless both the bundle declares `viewer.allow_active_code: true` and the operator consents with `--allow-active-code` or `OKF_LOOM_ALLOW_ACTIVE_CODE`.

## OKF metadata is not authority

`generated`, `verified`, source signals, and lifecycle are untrusted authored
metadata. Derived `human-reviewed`/`machine-confirmed` tiers do not authenticate
an actor or grant any mutation permission. A named human or process is not proof
that a check occurred. Sources can be non-navigable scope descriptors; unsafe URL
schemes are not actionable links in the metadata view.

Attested Computation fields are read, validated, and displayed only. Loom does
not fetch or execute computation/executor/attester resources, verify receipts,
or provide an attestation protocol or sandbox. The active-code consent gate is
separate and never inferred from trust metadata. Imports and migrations preserve
legacy evidence; `upgrade --actor` is an explicit original-producer assertion,
not an identity inferred from the migration operator.

## Retrieval GET boundary

`GET /__search`, its native-form adapter `GET /__search-form`, and
`GET /__context` are public, unauthenticated read surfaces
when a studio URL is reachable. They are replay-safe and do not write bundle
files, session state, derived capability declarations, or hybrid activation
state.

The native-form adapter accepts only the documented scalar search controls,
omits empty optional and mode-inactive filters, and redirects to strict
`/__search`. It does not relax direct API validation or grant mutation or
presence authority. Public pages do not poll or publish presence; local
read-only pages may observe presence but cannot publish it.

- Each request captures one immutable `Bundle` snapshot. Search/context
  evidence and revision headers come from that same generation even if a file
  watcher activates a newer bundle concurrently.
- The server caps the raw request target at 64 KiB before query parsing. It
  strictly decodes UTF-8 and percent escapes, rejects traversal/reserved-route
  aliases, unknown or duplicate scalar parameters, controls/bidirectional
  overrides, and mode-inapplicable fields. Queries are limited to 4096 Unicode
  code points and filter fields to 256; excess input is rejected, not
  truncated.
- Result limits are 1–100. Passage excerpts are at most 2000 code points,
  reasons at most 8, relation/evidence edges at most 20 with an omitted count,
  context neighbors 0–2, and context budgets 128–50000 estimated tokens.
  Search and context responses are capped at 4 MiB and are never byte-cut;
  deterministic tail omission is reported where the format allows it, or the
  request fails with `413`.
- Retrieval uses a bounded executor (four concurrent operations by default)
  and a 30-second deadline. Saturation or timeout returns a typed `503`.
  Timed-out computation retains its admission slot until it exits, preventing
  abandoned work from bypassing the concurrency bound.
- Public retrieval projects authored concept metadata only, with its own depth,
  collection, and text limits. Portable artifacts and live responses exclude
  filesystem roots, session comments, CSRF tokens, private runtime config, and
  other session state. Error responses do not expose stack traces or roots.
- Rendered fields and highlighted excerpts are escaped before HTML insertion.
  Live pages send CSP, `nosniff`, frame denial, and no-referrer headers; their
  CSP limits scripts to same-origin assets and the pinned enhancement CDN and
  does not allow `eval`. Static templates carry the corresponding meta CSP.
  The self-contained target necessarily permits its bundled inline script but
  sets `connect-src 'none'`. Generated scripts do not evaluate bundle text as
  code. The existing two-layer active-code gate remains separate and still
  applies to viewer overrides/plugins.

Portable passage manifests and chunks are schema-versioned and bound to one
bundle revision. Chunk names and identities include SHA-256 content hashes;
consumers validate the complete sequence, canonical paths and source
coordinates, hashes, graph data, and revision before publishing it. Partial,
mixed-revision, unsupported, duplicated, or tampered corpora fail closed.

## License boundary

okf-loom is licensed under Apache-2.0.
Do not submit code, documentation, or assets that cannot be distributed under Apache-2.0 unless the maintainers explicitly approve a separate license notice.
