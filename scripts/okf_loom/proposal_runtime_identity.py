"""Reviewed release identities for Proposal Review output-affecting runtimes.

The constants are generated from ``SOURCE_MANIFESTS`` during release review.
Production imports only these immutable values; it never hashes mutable source
files while serving a capture request.
"""
from __future__ import annotations

SOURCE_MANIFESTS = {
    "validator": (
        "scripts/okf_loom/_yaml_compat.py",  # Frontmatter/config parsing semantics.
        "scripts/okf_loom/config.py",
        "scripts/okf_loom/extensions.py",
        "scripts/okf_loom/model.py",
        # Graph diagnostics now distinguish navigation documents and use actual
        # renderer heading identities (including setext/underscore handling).
        "scripts/okf_loom/documents.py",
        "scripts/okf_loom/navigation.py",
        "scripts/okf_loom/markdown_blocks.py",
        "scripts/okf_loom/viewer/markdown.py",
        "scripts/okf_loom/parse.py",
        "scripts/okf_loom/paths.py",
        "scripts/okf_loom/validate.py",
        "scripts/okf_loom/proposal_snapshot.py",
        "scripts/okf_loom/__init__.py",
        "scripts/okf_loom/ignore.py",
        "scripts/okf_loom/metadata.py",
        "scripts/okf_loom/proposal_review/canonical.py",
    ),
    "serializer": (
        "scripts/okf_loom/_yaml_compat.py",  # Parser and round-trip dumper backend.
        "scripts/okf_loom/parse.py",
        "scripts/okf_loom/roundtrip.py",
        "scripts/okf_loom/proposal_review/operations.py",
        "scripts/okf_loom/metadata.py",
    ),
    "operation": (
        "scripts/okf_loom/proposal_review/activity.py",
        "scripts/okf_loom/proposal_review/activation.py",
        "scripts/okf_loom/proposal_review/ids.py",
        "scripts/okf_loom/io_utils.py",
        "scripts/okf_loom/proposal_review/operations.py",
        "scripts/okf_loom/proposal_review/ports.py",
        "scripts/okf_loom/proposal_review/preview.py",
        "scripts/okf_loom/proposal_review/staging.py",
        "scripts/okf_loom/proposal_review/schemas.py",
        "scripts/okf_loom/proposal_review/errors.py",
        "scripts/okf_loom/proposal_review/publication.py",
        "scripts/okf_loom/proposal_review/repository.py",
        "scripts/okf_loom/proposal_review/transaction_intent.py",
        "scripts/okf_loom/proposal_review/transactions.py",
        "scripts/okf_loom/session_authority.py",  # Activation's private-state admission.
        "scripts/okf_loom/proposal_review/canonical.py",
        "scripts/okf_loom/proposal_review/validation.py",
    ),
}

VALIDATOR_RULE_CODES = (
    "asset.missing", "asset.out_of_bundle", "capability.declared",
    "concept.bad_computation", "concept.bad_generated", "concept.bad_id",
    "concept.bad_sources", "concept.bad_status", "concept.bad_timestamp",
    "concept.bad_usage_window", "concept.bad_verified", "concept.body_too_large",
    "concept.citations_both_forms", "concept.duplicate_id",
    "concept.missing_recommended_keys", "concept.missing_runtime", "concept.missing_type",
    "concept.parse_failed", "concept.path_escapes_bundle", "concept.unreadable",
    "config.unparseable", "index.missing_for_directory",
    "index.non_root_frontmatter", "index.parse_failed",
    "index.unexpected_frontmatter", "link.broken", "link.form_inconsistency",
    "link.wikilink_present", "log.bad_date_heading", "log.not_newest_first",
    "reserved_file_escapes_bundle", "spec.version_major_mismatch",
    "spec.version_malformed",
    "spec.version_newer_minor", "spec.version_older_minor",
    "spec.version_undeclared", "tag.case_inconsistency",
)

# Updated only by an explicit reviewed release identity bump.
VALIDATOR_ID = "okf-validator@1.0+sha256:64d26d6947daa07bea5d3ac17a7a7747c6d169bd79ffb6027d4cae513f42a5df"
SERIALIZER_ID = "okf-roundtrip@1.0+sha256:9f4d826f237f8ba408d1e440655c5bfbd93f2275b479d733fe194c88f5bdeb82"
OPERATION_RUNTIME_ID = "proposal-ops@1.0+sha256:92ba44dc99500896d6fbc00ab9fd6022ade61b1327176996a49c7d0bb0230801"

__all__ = ["SOURCE_MANIFESTS", "VALIDATOR_RULE_CODES", "VALIDATOR_ID",
           "SERIALIZER_ID", "OPERATION_RUNTIME_ID"]
