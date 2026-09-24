---
type: How-to
title: Migrate across spec versions
description: Preview and explicitly apply the conservative OKF 0.1 to 0.2 migration while preserving authored evidence and recovering safely from interruption.
resource: /how-to/migrate_spec_version.md
tags: [migration, upgrade, spec-version, forward-compat]
timestamp: "2026-06-29T00:00:00Z"
---

# Migrate across spec versions

The runtime targets OKF **0.2**. Only `type` is always required; optional
`generated` mappings require `by`, not an invented actor or generation time.
Upstream [SPEC.md](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
§13 permits legacy `timestamp` fallback **only when `generated` is absent**.

Use a deliberate bundle directory and keep a version-control checkpoint or
backup before applying. This command does not validate or certify all content.

## Preview and check without writes

```bash
scripts/okf-loom upgrade path/to/bundle --preview --format json
scripts/okf-loom upgrade path/to/bundle --check
```

Plain `upgrade <bundle>` also previews supported versions. Preview/check create
no locks, runtime directories, caches, indexes, or logs. Their inventory is an
unlocked observation; apply rechecks it under the existing bundle transaction
lock. `revision` fingerprints the visible Markdown and scan configuration.

The migration is deliberately small:

- A declared `0.1` root index becomes `0.2`, preserving its authored body,
  unknown header fields, and comments. Index regeneration is never invoked.
- With an explicit `--actor`, a concept with a valid, offset-bearing legacy
  timestamp and **no** `generated` key gets an additive `{by, at}` mapping.
  The actor asserts who originally produced that content, not who runs upgrade.
  The legacy timestamp stays byte-for-byte as compatibility evidence.
- Without an actor, timestamps remain unchanged and the report is `partial`.
  Invalid timestamps, ambiguous YAML, or existing invalid generation metadata
  also remain for review. No existing `generated` value is repaired or replaced.
- Body citations, loom frontmatter citations, sources, and Context Pack
  citations are never rewritten or assigned guessed identities. Recognized
  legacy citation sections/fields are listed for manual review.

Undeclared bundles retain the current-version assumption and are not relabeled
or migrated. Declared `0.2` bundles may finish deferred timestamp work. Unsupported
conversions, malformed declarations, unsafe paths, malformed concepts, or invalid
scan configuration fail closed. Optional family absence remains legal.
Plain `upgrade <bundle>` has a legacy informational exception for valid
unsupported versions: it returns zero without a plan (`status: unsupported` in
JSON). Use explicit `--preview` or `--check` when support must be enforced.

## Apply only reviewed changes

If you know the original producer, repeat the preview with its explicit actor:

```bash
scripts/okf-loom upgrade path/to/bundle --preview --actor process:original-exporter --format json
scripts/okf-loom upgrade path/to/bundle --apply --actor process:original-exporter \
  --expected-revision <revision-from-preview>
```

Omit `--actor` when provenance is unknown. `--expected-revision` is optional but
recommended to reject source/configuration drift since preview; it binds the
snapshot, not the actor or other invocation options. Apply takes exclusive bundle
ownership and preflights the selected snapshot. Mutation requires Linux
`renameat2(RENAME_EXCHANGE)`; unsupported exchange fails closed, without an
ordinary-rename fallback. All source parents are pinned through no-follow
directory descriptors. Bounded source reads bind inode, metadata and bytes;
publication verifies both the candidate and the **actually displaced** source
against those identities. The declaration is exchanged only after concepts.
It creates the existing
advisory lock files only when there are changes. It does not attach a Studio,
fabricate activity attribution, create an undo history, or append a log.

Exit `0` means the supported preview/check/apply completed, **including partial
reports**. JSON `status`, `changes`, `deferred`, and `applied` distinguish results.
Exit `1` signals unsupported input, conflict, or failed migration; CLI usage
errors and missing bundles exit `2`. Review deferred items and then run:

```bash
scripts/okf-loom validate path/to/bundle
```

## Interruption and concurrency

The root declaration is written **last**. Migration no longer uses the generic
pathname batch writer/rollback. It registers each exchange attempt **before**
entering the syscall and reconciles both names after a caught failure, including
directory-open/fsync failures and `KeyboardInterrupt` after rename. Restoration
uses the actual displaced inode, not a stale preview copy. A differing inode
(even with identical bytes), changed content, or substituted parent is a conflict.

Private `.okf-upgrade-<filename>-<nonce>/entry` directories under source parents
retain exchange evidence until the batch settles. Successful rollback restores
original inodes/bytes when those slots are still owned. If a newer canonical
entry prevents safe restoration, or restoration/durability cannot be verified,
`upgrade.rollback_uncertain` reports the affected exchange-directory names and
preserves remaining evidence rather than deleting it. A race at reverse exchange
is also detected; displaced newer bytes are put back when possible, otherwise
retained for inspection. `upgrade.cleanup_uncertain` means the source batch was
published but cleanup/durability needs inspection; it does not claim rollback.
Some cleanup locations may already be absent when the failure was a final fsync.

This is **atomic exchange with verification**, not a kernel conditional CAS or
a bundle-wide filesystem transaction. Noncooperating readers can briefly see a
candidate before verification/rollback. Repeated noncooperating writes, hostile
same-UID access to private staging, namespace moves, and hard process/power loss
cannot be given a universal automatic-restoration guarantee. If a pinned parent
was moved, inspect that original directory too; recovery paths are not followed
through its replacement symlink. Each proposed concept change remains additive.

After interruption or uncertainty, first inspect the source **and every retained
exchange entry** against the checkpoint. An entry can contain an original,
migrated candidate, or a displaced newer edit; do not assume it is disposable.
There is no durable migration journal or automatic crash-recovery claim. After
reconciling that evidence, preview again and rerun with the same actor and a
**fresh** revision. Completed additions are skipped; no timestamps are refreshed.
Do not reuse a stale revision, blindly retry conflicts, or delete intervening edits. To undo
a completed migration, review and revert its additive changes against your
checkpoint; there is no automated downgrade.

## New content and imports

Bootstrap/init and newly bootstrapped imports declare `0.2`. Default
`write-concept` creation records `generated.by: okf-loom/2.0.0` and the current
UTC time only for newly produced content. Updates never inject these defaults;
`--no-defaults` omits them. Explicit `--timestamp` remains a legacy compatibility
input and never fabricates its historical producer. Import preserves typed
documents byte-for-byte and adds only missing type/title metadata to untyped
documents, not actors, verification, or generation timestamps.

Return to [How-to guides](/how-to/index.md).
