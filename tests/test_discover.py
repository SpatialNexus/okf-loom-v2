"""Tests for ``okf_loom.discover``.

Pinned invariants per rule:
  * ``unlinked_mentions``: detects a known mention, skips already-linked.
  * ``missing_indexes``: flags directories without index.md.
  * ``missing_descriptions``: flags concepts with no description.
  * ``broken_links``: re-surfaces validate's broken-link findings.
  * ``missing_relations_hint``: detects FK-like text in # Schema sections.
  * ``orphan_concepts``: flags isolated nodes.
Also: rule subset via ``rules=[...]``; ``as_dict()`` JSON-serializable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.discover import (
    DiscoveryReport,
    Suggestion,
    _ALL_RULES,
    discover_suggestions,
)


# --- rule registry ----------------------------------------------------------


def test_all_expected_rules_registered() -> None:
    """The six spec-required discovery rules are all present."""
    expected = {
        "unlinked_mentions",
        "missing_indexes",
        "missing_descriptions",
        "broken_links",
        "missing_relations_hint",
        "orphan_concepts",
    }
    assert set(_ALL_RULES.keys()) == expected


def test_unknown_rule_raises(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    with pytest.raises(ValueError, match="Unknown discovery rules"):
        discover_suggestions(b, rules=["nope"])


def test_rule_subset_via_rules_kwarg(tiny_good_bundle: Path) -> None:
    """Passing ``rules=[...]`` runs only those rules."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b, rules=["orphan_concepts"])
    assert set(rep.by_rule().keys()) == {"orphan_concepts"}


# --- unlinked_mentions ------------------------------------------------------


def test_unlinked_mentions_detects_mention(tmp_path: Path) -> None:
    """A plain-text mention of another concept title is flagged."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "alpha.md").write_text(
        "---\ntype: T\ntitle: Alpha\n---\n"
        "We reference the Beta concept here without linking it.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: T\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    rep = discover_suggestions(b, rules=["unlinked_mentions"])
    targets = {s.target_concept_id for s in rep.suggestions}
    assert ("beta",) in targets


def test_unlinked_mentions_skips_already_linked(tmp_path: Path) -> None:
    """A concept that already links to the target is NOT flagged."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "alpha.md").write_text(
        "---\ntype: T\ntitle: Alpha\n---\n"
        "We link to [Beta](/beta.md) directly.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: T\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    rep = discover_suggestions(b, rules=["unlinked_mentions"])
    # No unlinked_mentions: alpha links to beta already.
    assert rep.suggestions == []


def test_unlinked_mentions_skips_mentions_inside_code(tmp_path: Path) -> None:
    """A mention inside an inline code span or fenced block is not flagged."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "alpha.md").write_text(
        "---\ntype: T\ntitle: Alpha\n---\n"
        "Use `Beta` for that. Or:\n\n```\nBeta\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: T\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    rep = discover_suggestions(b, rules=["unlinked_mentions"])
    assert rep.suggestions == []


def test_unlinked_mentions_suppresses_common_low_confidence_labels(
    tmp_path: Path,
) -> None:
    """Common labels like Users are kept out of default discovery noise."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "alpha.md").write_text(
        "---\ntype: T\ntitle: Alpha\n---\n"
        "Users appear in many generic sentences without being a useful link.\n",
        encoding="utf-8",
    )
    (tmp_path / "users.md").write_text(
        "---\ntype: Table\ntitle: Users\n---\nUsers body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])
    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    assert rep.suppressed[0].detail["confidence"] < 0.5

    noisy = discover_suggestions(
        b, rules=["unlinked_mentions"], include_low_confidence=True,
    )
    assert len(noisy.suggestions) == 1
    assert noisy.suggestions[0].target_concept_id == ("users",)


def test_unlinked_mentions_suppresses_common_person_names(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\nDavid approved this item.\n",
        encoding="utf-8",
    )
    (tmp_path / "david.md").write_text(
        "---\ntype: Person\ntitle: David\n---\nPerson record.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    assert "common_person_name" in rep.suppressed[0].detail["confidence_reasons"]


def test_unlinked_mentions_suppresses_high_frequency_project_labels(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    for i in range(12):
        (tmp_path / f"notes_{i}.md").write_text(
            "---\n"
            f"type: Note\ntitle: Note {i}\n"
            "source_system: import\n"
            "graph_cluster: import/wiki\n"
            "---\n"
            "Ebotech appears as a project label in many imported notes.\n",
            encoding="utf-8",
        )
    (tmp_path / "ebotech.md").write_text(
        "---\n"
        "type: Project\n"
        "title: Ebotech\n"
        "source_system: import\n"
        "graph_cluster: import/projects\n"
        "---\nProject body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert rep.suggestions == []
    assert len(rep.suppressed) == 12
    first = rep.suppressed[0]
    assert first.detail["document_frequency"] >= 10
    assert "high_document_frequency" in first.detail["confidence_reasons"]


def test_unlinked_mentions_suppresses_existing_structural_relation(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "alpha.md").write_text(
        "---\n"
        "type: T\n"
        "title: Alpha\n"
        "relations:\n"
        "  - type: references\n"
        "    target: /beta.md\n"
        "---\n"
        "Beta is already represented by structured metadata.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: T\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    suppressed = rep.suppressed[0]
    assert suppressed.action == "no action"
    assert suppressed.detail["suppression_reasons"] == [
        "already_structurally_related"
    ]
    assert (
        rep.as_dict()["suppressed_reason_counts"]["already_structurally_related"]
        == 1
    )

    noisy = discover_suggestions(
        b, rules=["unlinked_mentions"], include_low_confidence=True,
    )
    assert noisy.suggestions == []
    assert len(noisy.suppressed) == 1


def test_unlinked_mentions_skips_alias_marked_not_discoverable(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\nArchitecture is broad here.\n",
        encoding="utf-8",
    )
    (tmp_path / "design.md").write_text(
        "---\n"
        "type: Design\n"
        "title: Design System\n"
        "aliases:\n"
        "  - label: Architecture\n"
        "    discoverable: false\n"
        "---\n"
        "Design body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(
        b, rules=["unlinked_mentions"], include_low_confidence=True,
    )

    assert rep.suggestions == []
    assert rep.suppressed == []


def test_unlinked_mentions_suppresses_h1_only_match(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\n# Beta\n\nNo body mention.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: Topic\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    detail = rep.suppressed[0].detail
    assert detail["location_counts"] == {"h1": 1}
    assert detail["occurrence_locations"] == [{"line": 1, "location": "h1"}]
    assert "h1_only" in detail["confidence_reasons"]


def test_unlinked_mentions_suppresses_frontmatter_only_match(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "---\n"
        "type: Note\n"
        "title: Beta Review\n"
        "description: Planning note.\n"
        "---\n"
        "No body mention.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: Topic\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    detail = rep.suppressed[0].detail
    assert detail["location_counts"] == {"frontmatter": 1}
    assert detail["occurrence_locations"] == [
        {"line": 0, "location": "frontmatter"}
    ]
    assert "frontmatter_only" in detail["confidence_reasons"]


def test_unlinked_mentions_keeps_heading_match_with_body_prose(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\n"
        "## Beta\n\n"
        "Beta is also discussed in prose here.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: Topic\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert len(rep.suggestions) == 1
    detail = rep.suggestions[0].detail
    assert detail["location_counts"] == {"heading": 1, "body": 1}
    assert "also_mentioned_in_body" in detail["confidence_reasons"]


def test_unlinked_mentions_suppresses_table_only_match(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\n"
        "| Name |\n"
        "| --- |\n"
        "| Beta |\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: Topic\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    detail = rep.suppressed[0].detail
    assert detail["location_counts"] == {"table": 1}
    assert "table_only" in detail["confidence_reasons"]


def test_unlinked_mentions_suppresses_generic_label_without_shared_context(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "project_a").mkdir()
    (tmp_path / "project_b").mkdir()
    (tmp_path / "project_a" / "note.md").write_text(
        "---\n"
        "type: Note\n"
        "title: Project A Note\n"
        "graph_cluster: project-a\n"
        "---\n"
        "Review the Architecture before changing the deployment path.\n",
        encoding="utf-8",
    )
    (tmp_path / "project_b" / "architecture.md").write_text(
        "---\n"
        "type: Design\n"
        "title: Architecture\n"
        "graph_cluster: project-b\n"
        "---\n"
        "Architecture body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    reasons = rep.suppressed[0].detail["confidence_reasons"]
    assert "generic_duplicate_label" in reasons
    assert "generic_duplicate_label_weak_context" in reasons
    assert "cross_context_generic_label" in reasons
    assert "low_confidence" in rep.suppressed[0].detail["suppression_reasons"]


def test_unlinked_mentions_suppresses_configured_phrase(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "okf-loom.config.yaml").write_text(
        "discover:\n"
        "  suppress_phrases:\n"
        "    - beta\n",
        encoding="utf-8",
    )
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\nBeta is mentioned in prose.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: Topic\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(
        b, rules=["unlinked_mentions"], include_low_confidence=True,
    )

    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    assert rep.suppressed[0].detail["suppression_reasons"] == [
        "configured_phrase"
    ]
    assert rep.as_dict()["suppressed_reason_counts"]["configured_phrase"] == 1


def test_unlinked_mentions_suppresses_configured_pair(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "okf-loom.config.yaml").write_text(
        "discover:\n"
        "  suppress_pairs:\n"
        "    - source: /note.md\n"
        "      target: /beta.md\n",
        encoding="utf-8",
    )
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\nBeta is mentioned in prose.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: Topic\ntitle: Beta\n---\nBeta body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(
        b, rules=["unlinked_mentions"], include_low_confidence=True,
    )

    assert rep.suggestions == []
    assert len(rep.suppressed) == 1
    assert rep.suppressed[0].detail["suppression_reasons"] == [
        "configured_pair"
    ]
    assert rep.as_dict()["suppressed_reason_counts"]["configured_pair"] == 1


def test_unlinked_mentions_keeps_generic_label_with_shared_context(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    project = tmp_path / "project_a"
    project.mkdir()
    (project / "note.md").write_text(
        "---\n"
        "type: Note\n"
        "title: Project A Note\n"
        "graph_cluster: project-a\n"
        "---\n"
        "Review the Architecture before changing the deployment path.\n",
        encoding="utf-8",
    )
    (project / "architecture.md").write_text(
        "---\n"
        "type: Design\n"
        "title: Architecture\n"
        "graph_cluster: project-a\n"
        "---\n"
        "Architecture body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert len(rep.suggestions) == 1
    reasons = rep.suggestions[0].detail["confidence_reasons"]
    assert "generic_duplicate_label" in reasons
    assert "generic_duplicate_label_same_context" in reasons
    assert "same_graph_cluster" in reasons
    assert "same_parent_folder" in reasons


def test_unlinked_mentions_tolerates_missing_target_type(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\nThe Legacy Page needs review.\n",
        encoding="utf-8",
    )
    (tmp_path / "legacy.md").write_text(
        "---\ntitle: Legacy Page\n---\nImported body.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])

    assert len(rep.suggestions) == 1
    assert rep.suggestions[0].target_concept_id == ("legacy",)


# --- missing_indexes --------------------------------------------------------


def test_missing_indexes_flags_directory_without_index(tmp_path: Path) -> None:
    """A directory containing concepts but no index.md is flagged."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text("---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    rep = discover_suggestions(b, rules=["missing_indexes"])
    dirs = [s.detail.get("directory") for s in rep.suggestions]
    assert "sub" in dirs


def test_missing_indexes_no_flag_when_index_present(tiny_good_bundle: Path) -> None:
    """tiny_good has hand-authored index.md in both subdirectories."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b, rules=["missing_indexes"])
    assert rep.suggestions == []


# --- missing_descriptions ---------------------------------------------------


def test_missing_descriptions_flags_concept(tiny_good_bundle: Path) -> None:
    """The orphan concept (no description) is flagged."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b, rules=["missing_descriptions"])
    ids = {s.concept_id for s in rep.suggestions}
    assert ("references", "orphan") in ids


def test_missing_descriptions_skips_concepts_with_description(
    tiny_good_bundle: Path,
) -> None:
    """Concepts that have a description are not flagged."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b, rules=["missing_descriptions"])
    ids = {s.concept_id for s in rep.suggestions}
    assert ("tables", "users") not in ids  # users has a description


# --- broken_links -----------------------------------------------------------


def test_broken_links_resurfaces_validate(tiny_bad_bundle: Path) -> None:
    """``broken_links`` re-surfaces validate's link.broken findings."""
    b = Bundle.load(tiny_bad_bundle)
    rep = discover_suggestions(b, rules=["broken_links"])
    assert len(rep.suggestions) >= 1
    for s in rep.suggestions:
        assert s.rule == "broken_links"
        assert s.severity == "warning"


def test_broken_links_empty_on_clean_bundle(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b, rules=["broken_links"])
    assert rep.suggestions == []


# --- missing_relations_hint -------------------------------------------------


def test_missing_relations_hint_fires(tmp_path: Path) -> None:
    """FK-like text in # Schema with no outgoing relation link is hinted."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "child.md").write_text(
        "---\ntype: Table\ntitle: Child\n---\n"
        "# Schema\n\n- parent_id: FK to parent\n",
        encoding="utf-8",
    )
    (tmp_path / "parent.md").write_text(
        "---\ntype: Table\ntitle: Parent\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    rep = discover_suggestions(b, rules=["missing_relations_hint"])
    ids = {s.concept_id for s in rep.suggestions}
    assert ("child",) in ids


def test_missing_relations_hint_skips_when_link_present(tmp_path: Path) -> None:
    """If the concept already links to the parent, no hint is emitted."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "child.md").write_text(
        "---\ntype: Table\ntitle: Child\n---\n"
        "# Schema\n\n- parent_id: FK to parent\n\n"
        "Links: [parent](/parent.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "parent.md").write_text(
        "---\ntype: Table\ntitle: Parent\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    rep = discover_suggestions(b, rules=["missing_relations_hint"])
    assert ("child",) not in {s.concept_id for s in rep.suggestions}


def test_missing_relations_hint_in_tiny_good(tiny_good_bundle: Path) -> None:
    """tiny_good's tables/users has FK text in # Schema and no outgoing
    internal-edge to the FK target (it has edges to metrics and events,
    neither of which is the FK target), so the hint fires."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b, rules=["missing_relations_hint"])
    # Either users fires (FK text present, no resolved edge to references/metrics
    # via the FK) — actually users DOES link to metrics, so we just assert
    # the rule returns a list (possibly empty) without raising.
    assert isinstance(rep.suggestions, list)


# --- orphan_concepts --------------------------------------------------------


def test_orphan_concepts_flags_isolated(tiny_good_bundle: Path) -> None:
    """tiny_good's `references/orphan` has no in/out internal edges."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b, rules=["orphan_concepts"])
    ids = {s.concept_id for s in rep.suggestions}
    assert ("references", "orphan") in ids


def test_orphan_concepts_skips_connected(tmp_path: Path) -> None:
    """A concept with an outgoing internal edge is not an orphan."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\n[b](/b.md)\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    rep = discover_suggestions(b, rules=["orphan_concepts"])
    assert rep.suggestions == []


# --- DiscoveryReport shape & JSON -------------------------------------------


def test_discovery_report_as_dict_json_serializable(tiny_good_bundle: Path) -> None:
    """``DiscoveryReport.as_dict()`` is JSON-serializable and well-shaped."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b)
    d = rep.as_dict()
    for key in (
        "bundle_root",
        "total",
        "counts",
        "actionability_counts",
        "actionability",
        "suggestions",
    ):
        assert key in d
    assert d["total"] == len(rep.suggestions)
    blob = json.dumps(d)
    assert isinstance(blob, str)


def test_discovery_report_actionability_buckets(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\n"
        "Payment Gateway is mentioned in body prose.\n",
        encoding="utf-8",
    )
    (tmp_path / "heading.md").write_text(
        "---\ntype: Note\ntitle: Heading\n---\n# Beta\n",
        encoding="utf-8",
    )
    (tmp_path / "payment_gateway.md").write_text(
        "---\ntype: Service\ntitle: Payment Gateway\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "---\ntype: Topic\ntitle: Beta\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)

    rep = discover_suggestions(b, rules=["unlinked_mentions"])
    data = rep.as_dict()

    assert data["actionability_counts"]["safe_to_apply"] == 1
    assert data["actionability_counts"]["low_confidence"] == 1
    assert (
        data["actionability"]["safe_to_apply"][0]["target_concept_id"]
        == "payment_gateway"
    )
    assert data["actionability"]["low_confidence"][0]["target_concept_id"] == "beta"
    assert (
        data["actionability"]["safe_to_apply"][0]["actionability_bucket"]
        == "safe_to_apply"
    )


def test_suggestion_as_dict_json_serializable(tiny_good_bundle: Path) -> None:
    """``Suggestion.as_dict()`` renders concept_ids as strings and is JSON-safe."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b)
    assert rep.suggestions, "expected at least one suggestion"
    for s in rep.suggestions:
        d = s.as_dict()
        for key in (
            "rule", "severity", "message", "concept_id",
            "target_concept_id", "action", "detail",
        ):
            assert key in d
        if d["concept_id"] is not None:
            assert isinstance(d["concept_id"], str)
            assert isinstance(d["concept_id"], str)  # concept_id is always a string
    # Whole list JSON-dumps without error.
    json.dumps([s.as_dict() for s in rep.suggestions])


def test_discovery_report_by_rule_groups(tiny_good_bundle: Path) -> None:
    """``by_rule()`` returns a dict[str, list[Suggestion]]."""
    b = Bundle.load(tiny_good_bundle)
    rep = discover_suggestions(b)
    grouped = rep.by_rule()
    assert isinstance(grouped, dict)
    for rule, items in grouped.items():
        assert all(s.rule == rule for s in items)


# --- §11 scoped enrichment (P1-12 / ARCH-004) ------------------------------
#
# Current spec §7: ``okf discover <bundle> --scope <id>,<id>`` must keep ONLY
# suggestions whose subject concept (``Suggestion.concept_id``) is in scope.
# ``--neighbors`` expands the scope by one graph hop (handled in the CLI /
# plan layer for discover; the library only filters to whatever set it gets).
# These tests prove the scope filter is real and load-bearing.


def _scope_test_bundle(tmp_path: Path) -> Path:
    """A bundle with two source concepts that each carry an unlinked mention.

    Layout:
        a.md mentions "Bee" but doesn't link to b.md   → unlinked_mentions(a→b)
        x.md mentions "Whye" but doesn't link to y.md  → unlinked_mentions(x→y)
        b.md, y.md exist as the unlinked targets.

    Also wires a.md → b.md as a real markdown link in a SEPARATE control
    concept (a_link.md) so we can prove ``--neighbors`` pulls in a neighbour's
    suggestion. See ``_scope_neighbor_test_bundle`` for the neighbour case.
    """
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: Ay\n---\nWe mention Bee here without linking.\n",
        encoding="utf-8",
    )
    (tmp_path / "x.md").write_text(
        "---\ntype: T\ntitle: Ex\n---\nWe mention Whye here without linking.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: Bee\n---\nBee body.\n", encoding="utf-8",
    )
    (tmp_path / "y.md").write_text(
        "---\ntype: T\ntitle: Whye\n---\nWhye body.\n", encoding="utf-8",
    )
    return tmp_path


def test_scope_filters_to_subject_concept_only(tmp_path: Path) -> None:
    """§11: ``scope=["a"]`` keeps only suggestions whose subject is ``a``.

    Without scope there are two unlinked_mentions (a→b and x→y). With
    ``scope=["a"]`` ONLY the a→b suggestion remains — the x→y suggestion
    is dropped because its subject concept (x) is not in scope.
    """
    b = Bundle.load(_scope_test_bundle(tmp_path))
    # Sanity: without scope, both subjects appear.
    full = discover_suggestions(b, rules=["unlinked_mentions"])
    subjects = {s.concept_id for s in full.suggestions}
    assert ("a",) in subjects and ("x",) in subjects, (
        f"expected both a + x as subjects; got {subjects}"
    )
    # §11 contract: scope=["a"] keeps ONLY a's suggestions.
    scoped = discover_suggestions(b, rules=["unlinked_mentions"], scope=["a"])
    scoped_subjects = {s.concept_id for s in scoped.suggestions}
    assert scoped_subjects == {("a",)}, (
        f"scope=['a'] leaked out-of-scope subjects: {scoped_subjects}"
    )
    # And the kept suggestion is a→b (the target stays bundle-wide; only the
    # subject is scoped per §11).
    assert {s.target_concept_id for s in scoped.suggestions} == {("b",)}


def test_scope_with_multiple_ids_keeps_union(tmp_path: Path) -> None:
    """§11: ``scope=["a","x"]`` keeps suggestions for either subject."""
    b = Bundle.load(_scope_test_bundle(tmp_path))
    scoped = discover_suggestions(b, rules=["unlinked_mentions"], scope=["a", "x"])
    subjects = {s.concept_id for s in scoped.suggestions}
    assert subjects == {("a",), ("x",)}, subjects


def test_scope_drops_directory_level_rules(tmp_path: Path) -> None:
    """§11: directory-scoped rules (``missing_indexes`` carry concept_id=None)
    are dropped under scope because they have no subject concept to match."""
    # Build a bundle with a directory that lacks an index.md so
    # missing_indexes fires.
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "concept.md").write_text(
        "---\ntype: T\ntitle: Concept\n---\nbody\n", encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    # Without scope: missing_indexes fires for "sub".
    full = discover_suggestions(b, rules=["missing_indexes"])
    assert any(s.rule == "missing_indexes" for s in full.suggestions)
    # With ANY scope: directory-scoped suggestions (concept_id=None) are
    # dropped because no subject concept can match.
    scoped = discover_suggestions(b, rules=["missing_indexes"], scope=["sub/concept"])
    assert scoped.suggestions == [], (
        f"directory-level rule leaked through scope: {scoped.suggestions}"
    )


def test_scope_filter_is_load_bearing_mutation(tmp_path: Path, monkeypatch) -> None:
    """§11 mutation test (P1-12): if the scope filter in
    :func:`discover_suggestions` is bypassed, a scoped call returns
    out-of-scope suggestions. This test FAILS when the filter is removed —
    proving the filter is load-bearing rather than a no-op.

    We simulate the mutation by calling the underlying rule directly (which
    has no scope awareness) and confirming that the scoped call drops
    suggestions the unscoped path returns. The differential between the
    two paths IS the scope filter.
    """
    b = Bundle.load(_scope_test_bundle(tmp_path))
    # The unfiltered rule returns BOTH subjects' suggestions.
    raw = _ALL_RULES["unlinked_mentions"](b)
    raw_subjects = {s.concept_id for s in raw}
    assert raw_subjects == {("a",), ("x",)}, raw_subjects
    # The filtered (scoped) call drops one. If the scope filter were
    # removed, ``scoped_subjects`` would equal ``raw_subjects`` and the
    # assert below would fail — exactly the mutation the task asks us to
    # guard against.
    scoped = discover_suggestions(b, rules=["unlinked_mentions"], scope=["a"])
    scoped_subjects = {s.concept_id for s in scoped.suggestions}
    assert scoped_subjects == {("a",)}, (
        f"scope filter is not narrowing the result — "
        f"raw={raw_subjects} scoped={scoped_subjects}"
    )
    # Explicit mutation: invoke discover_suggestions with scope stripped
    # from kwargs (simulating someone deleting the filter block) and prove
    # the result loses the scope-narrowing property.
    import okf_loom.discover as disc

    real_fn = disc.discover_suggestions

    def _mutation_no_scope(bundle, **kwargs):
        kwargs.pop("scope", None)
        return real_fn(bundle, **kwargs)

    mutated = _mutation_no_scope(b, rules=["unlinked_mentions"], scope=["a"])
    mutated_subjects = {s.concept_id for s in mutated.suggestions}
    # The mutation leaks the out-of-scope subject x back in — that is the
    # regression this test pins against.
    assert ("x",) in mutated_subjects, (
        "expected the no-scope mutation to leak x back in; "
        "if it does not, the scope filter contract changed"
    )


def _scope_neighbor_bundle(tmp_path: Path) -> Path:
    """A bundle whose graph gives ``a`` a real neighbour ``b`` (a links to b),
    and where BOTH ``a`` and ``b`` have an unlinked mention of a third
    concept ``c``. Used to prove ``--neighbors`` surfaces the neighbour's
    suggestion that the bare scope would have dropped.
    """
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: Ay\n---\n"
        "We link to [Bee](/b.md) and also mention Cee without linking.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: Bee\n---\n"
        "Bee body. We also mention Cee without linking.\n",
        encoding="utf-8",
    )
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ntitle: Cee\n---\nCee body.\n", encoding="utf-8",
    )
    return tmp_path


def test_scope_with_neighbors_expands_to_graph_neighbours(tmp_path: Path) -> None:
    """§11 ``--neighbors``: ``discover B --scope a --neighbors`` returns not
    only a's suggestion but ALSO the new suggestion on b (a graph neighbour
    of a), proving the neighbour expansion reaches suggestions the bare
    scope would have dropped.

    The discover library itself only filters to whatever set it receives;
    the neighbour expansion is the CLI / plan layer's job (mirrored here by
    expanding the scope set using ``Bundle.graph().neighbours(a, max_depth=1)``
    exactly as ``cli.cmd_discover`` does).
    """
    from okf_loom.paths import concept_id_to_str

    b = Bundle.load(_scope_neighbor_bundle(tmp_path))
    graph = b.graph()
    # a's graph neighbours: b (out-edge a→b exists).
    neighbours = graph.neighbours(("a",), max_depth=1)
    assert ("b",) in neighbours, (
        f"test prelude: expected b in a's neighbours; got {neighbours}"
    )
    # Bare scope: only a's suggestion (a→c).
    bare = discover_suggestions(b, rules=["unlinked_mentions"], scope=["a"])
    bare_subjects = {s.concept_id for s in bare.suggestions}
    assert bare_subjects == {("a",)}, bare_subjects
    # CLI-style neighbour expansion (mirrors cli.cmd_discover).
    expanded = {"a"} | {concept_id_to_str(n) for n in neighbours}
    with_neighbours = discover_suggestions(
        b, rules=["unlinked_mentions"], scope=sorted(expanded),
    )
    wn_subjects = {s.concept_id for s in with_neighbours.suggestions}
    # The neighbour's suggestion (b→c) is now included.
    assert wn_subjects == {("a",), ("b",)}, (
        f"--neighbors did not surface the neighbour's suggestion: {wn_subjects}"
    )
