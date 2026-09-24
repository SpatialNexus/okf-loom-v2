"""Tests for advisory graph-quality reporting."""
from __future__ import annotations

from pathlib import Path

from okf_loom.graph_quality import analyze_graph_quality
from okf_loom.model import Bundle


def _write_concept(path: Path, *, typ: str, title: str, body: str = "body\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {typ}\ntitle: {title}\n---\n{body}",
        encoding="utf-8",
    )


def test_graph_quality_flags_generic_types_and_orphans(tmp_path: Path) -> None:
    for i in range(6):
        _write_concept(tmp_path / f"page_{i}.md", typ="Wiki Page", title=f"Page {i}")

    report = analyze_graph_quality(Bundle.load(tmp_path))
    codes = {finding.code for finding in report.findings}

    assert "type.overused_generic" in codes
    assert "graph.orphans" in codes
    assert "graph.disconnected_components" in codes
    assert report.metrics["concept_count"] == 6
    assert report.metrics["orphan_count"] == 6


def test_graph_quality_flags_duplicate_titles(tmp_path: Path) -> None:
    _write_concept(tmp_path / "a.md", typ="Service", title="Customer API")
    _write_concept(tmp_path / "b.md", typ="Service", title="customer-api")

    report = analyze_graph_quality(Bundle.load(tmp_path))
    duplicate = [f for f in report.findings if f.code == "title.duplicate"]

    assert duplicate
    assert set(duplicate[0].concept_ids) == {"a", "b"}


def test_graph_quality_counts_optional_metadata_without_requiring_it(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: Runbook\n"
        "title: Deploy\n"
        "graph_cluster: ops/deploy\n"
        "source_system: local\n"
        "aliases: [deployment]\n"
        "---\nbody\n",
        encoding="utf-8",
    )

    report = analyze_graph_quality(Bundle.load(tmp_path))

    assert report.metrics["cluster_key_counts"]["graph_cluster"] == 1
    assert report.metrics["cluster_key_counts"]["source_system"] == 1
    assert report.metrics["governed_key_counts"]["aliases"] == 1
