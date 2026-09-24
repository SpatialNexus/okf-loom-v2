"""Advisory graph-quality reporting for OKF bundles.

This module is deliberately separate from ``validate``. OKF conformance stays
small (``type`` is the only hard-required concept key); graph quality reports
whether the bundle is likely to be useful in graph/search views.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .model import Bundle
from .paths import ConceptId, concept_id_to_str


GENERIC_TYPES = {
    "document",
    "wiki page",
    "index",
    "page",
}

GOVERNED_KEYS = ("aliases", "entities", "provenance", "citations", "relations")
CLUSTER_KEYS = ("graph_cluster", "source_system")
HIERARCHY_REL_TYPES = {
    "part_of",
    "contains",
    "parent_of",
    "child_of",
    "belongs_to",
    "section_of",
    "has_section",
}


@dataclass
class QualityFinding:
    """One advisory graph-quality finding."""

    code: str
    severity: str
    message: str
    concept_ids: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "concept_ids": list(self.concept_ids),
            "detail": dict(self.detail),
        }


@dataclass
class GraphQualityReport:
    """Advisory graph-quality report."""

    bundle_root: str
    metrics: dict[str, Any]
    findings: list[QualityFinding] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {"warning": 0, "info": 0}
        for finding in self.findings:
            out[finding.severity] = out.get(finding.severity, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle_root": self.bundle_root,
            "counts": self.counts(),
            "metrics": dict(self.metrics),
            "findings": [finding.as_dict() for finding in self.findings],
        }


def analyze_graph_quality(bundle: Bundle) -> GraphQualityReport:
    """Return an advisory graph-quality report for ``bundle``.

    The report never changes bundle validity. Findings are deterministic and
    generic: they rely only on OKF frontmatter, links, typed relations, and a
    small set of optional metadata keys that are safe to ignore.
    """
    graph = bundle.graph()
    concepts = sorted(bundle.concepts.values(), key=lambda c: c.id)
    n = len(concepts)
    resolved_edges = [
        link for link in graph.edges
        if link.target is not None and link.target in graph.concept_ids
    ]
    components = _components(graph.concept_ids, resolved_edges)
    component_sizes = sorted((len(c) for c in components), reverse=True)
    orphan_ids = sorted(
        cid for cid in graph.concept_ids
        if not any(cid in comp and len(comp) > 1 for comp in components)
    )

    type_counts: dict[str, int] = {}
    for concept in concepts:
        typ = concept.type or ""
        type_counts[typ] = type_counts.get(typ, 0) + 1

    governed_counts = {
        key: sum(1 for concept in concepts if _non_empty_list(concept.frontmatter.get(key)))
        for key in GOVERNED_KEYS
    }
    cluster_counts = {
        key: sum(1 for concept in concepts if _non_empty_scalar(concept.frontmatter.get(key)))
        for key in CLUSTER_KEYS
    }
    hierarchy_relation_count = sum(_hierarchy_relation_count(c.frontmatter) for c in concepts)

    metrics = {
        "concept_count": n,
        "resolved_edge_count": len(resolved_edges),
        "unresolved_link_count": len(graph.unresolved),
        "external_link_count": len(graph.external),
        "type_counts": dict(sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "governed_key_counts": governed_counts,
        "cluster_key_counts": cluster_counts,
        "hierarchy_relation_count": hierarchy_relation_count,
        "component_count": len(components),
        "component_sizes": component_sizes,
        "orphan_count": len(orphan_ids),
    }
    findings: list[QualityFinding] = []
    findings.extend(_generic_type_findings(concepts, type_counts, n))
    findings.extend(_duplicate_title_findings(concepts))
    findings.extend(_metadata_findings(n, governed_counts, cluster_counts, hierarchy_relation_count))
    findings.extend(_connectivity_findings(n, components, orphan_ids))

    findings.sort(key=lambda f: (0 if f.severity == "warning" else 1, f.code, f.message))
    return GraphQualityReport(
        bundle_root=str(bundle.root),
        metrics=metrics,
        findings=findings,
    )


def format_text(report: GraphQualityReport) -> str:
    """Render ``report`` as concise human-readable text."""
    counts = report.counts()
    metrics = report.metrics
    lines = [
        f"Graph quality: {report.bundle_root}",
        (
            f"# {metrics['concept_count']} concepts, "
            f"{metrics['resolved_edge_count']} resolved edges, "
            f"{metrics['component_count']} component(s), "
            f"{metrics['orphan_count']} orphan(s)"
        ),
        f"# {counts.get('warning', 0)} warning(s), {counts.get('info', 0)} info",
    ]
    for finding in report.findings:
        lines.append(f"  [{finding.severity.upper():7}] {finding.code}")
        lines.append(f"            {finding.message}")
        if finding.concept_ids:
            shown = finding.concept_ids[:8]
            suffix = "" if len(finding.concept_ids) <= 8 else f" (+{len(finding.concept_ids) - 8} more)"
            lines.append(f"            concepts: {', '.join(shown)}{suffix}")
    return "\n".join(lines) + "\n"


def _components(concept_ids: set[ConceptId], edges: list[Any]) -> list[set[ConceptId]]:
    adj: dict[ConceptId, set[ConceptId]] = {cid: set() for cid in concept_ids}
    for link in edges:
        if link.target is None:
            continue
        adj.setdefault(link.source, set()).add(link.target)
        adj.setdefault(link.target, set()).add(link.source)

    seen: set[ConceptId] = set()
    comps: list[set[ConceptId]] = []
    for cid in sorted(concept_ids):
        if cid in seen:
            continue
        stack = [cid]
        seen.add(cid)
        comp: set[ConceptId] = set()
        while stack:
            cur = stack.pop()
            comp.add(cur)
            for nb in sorted(adj.get(cur, set())):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        comps.append(comp)
    return comps


def _generic_type_findings(concepts: list[Any], type_counts: dict[str, int], n: int) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    if n == 0:
        return findings
    for typ, count in sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        if typ.strip().lower() not in GENERIC_TYPES:
            continue
        ratio = count / n
        if count < 5 and ratio < 0.25:
            continue
        ids = [
            concept_id_to_str(c.id)
            for c in concepts
            if (c.type or "").strip().lower() == typ.strip().lower()
        ]
        findings.append(QualityFinding(
            code="type.overused_generic",
            severity="warning",
            message=(
                f"{count} of {n} concepts use the generic type {typ!r}. "
                "Use type for the kind of thing, and move source-system or "
                "dataset labels into tags or custom metadata."
            ),
            concept_ids=ids,
            detail={"type": typ, "count": count, "ratio": round(ratio, 3)},
        ))
    return findings


def _duplicate_title_findings(concepts: list[Any]) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    by_norm: dict[str, list[Any]] = {}
    titles: list[tuple[str, str, Any]] = []
    for concept in concepts:
        title = (concept.title or concept.id[-1]).strip()
        if not title:
            continue
        norm = _normalise_title(title)
        by_norm.setdefault(norm, []).append(concept)
        titles.append((title, norm, concept))

    for norm, group in sorted(by_norm.items()):
        if len(group) < 2:
            continue
        ids = [concept_id_to_str(c.id) for c in sorted(group, key=lambda c: c.id)]
        findings.append(QualityFinding(
            code="title.duplicate",
            severity="warning",
            message="Multiple concepts have the same normalized title.",
            concept_ids=ids,
            detail={"normalized_title": norm},
        ))

    # Near-duplicate scan is capped to keep reports cheap on huge bundles.
    if 1 < len(titles) <= 600:
        pairs: list[tuple[str, str, float]] = []
        for i, (a_title, a_norm, a_concept) in enumerate(titles):
            if len(a_norm) < 6:
                continue
            for b_title, b_norm, b_concept in titles[i + 1:]:
                if a_norm == b_norm or len(b_norm) < 6:
                    continue
                ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
                if ratio >= 0.92:
                    pairs.append((
                        concept_id_to_str(a_concept.id),
                        concept_id_to_str(b_concept.id),
                        round(ratio, 3),
                    ))
                    if len(pairs) >= 20:
                        break
            if len(pairs) >= 20:
                break
        if pairs:
            flattened = sorted({cid for a, b, _ in pairs for cid in (a, b)})
            findings.append(QualityFinding(
                code="title.near_duplicate",
                severity="info",
                message="Some concept titles are very similar; merge or disambiguate if they represent the same thing.",
                concept_ids=flattened,
                detail={"pairs": pairs},
            ))
    return findings


def _metadata_findings(
    n: int,
    governed_counts: dict[str, int],
    cluster_counts: dict[str, int],
    hierarchy_relation_count: int,
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    if n == 0:
        return findings

    missing_governed = [
        key for key in ("aliases", "entities", "provenance")
        if governed_counts.get(key, 0) == 0
    ]
    if n >= 10 and missing_governed:
        findings.append(QualityFinding(
            code="metadata.low_governed_coverage",
            severity="info",
            message=(
                "No concepts carry "
                + ", ".join(missing_governed)
                + ". These keys are optional, but they make search, detail panels, and imported-source review stronger."
            ),
            detail={"missing_keys": missing_governed},
        ))

    if n >= 10 and hierarchy_relation_count == 0:
        findings.append(QualityFinding(
            code="relations.no_hierarchy_signal",
            severity="info",
            message=(
                "No explicit hierarchy relations were found. Folder paths still work, "
                "but large bundles become easier to navigate when concepts use "
                "part_of/contains/belongs_to style relations where appropriate."
            ),
        ))

    cluster_coverage = cluster_counts.get("graph_cluster", 0) / n
    source_coverage = cluster_counts.get("source_system", 0) / n
    if n >= 20 and cluster_coverage < 0.5:
        findings.append(QualityFinding(
            code="metadata.low_graph_cluster_coverage",
            severity="info",
            message=(
                "Fewer than half of concepts carry graph_cluster. This key is optional, "
                "but useful for imported or mixed-source bundles where folder/type alone is not enough."
            ),
            detail={"coverage": round(cluster_coverage, 3)},
        ))
    if n >= 20 and source_coverage == 0:
        findings.append(QualityFinding(
            code="metadata.no_source_system",
            severity="info",
            message=(
                "No source_system metadata was found. For imported bundles, a source_system key "
                "helps clustering and provenance review without changing the OKF wire format."
            ),
        ))
    return findings


def _connectivity_findings(
    n: int,
    components: list[set[ConceptId]],
    orphan_ids: list[ConceptId],
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    if n == 0:
        return findings
    component_sizes = sorted((len(c) for c in components), reverse=True)
    largest_ratio = component_sizes[0] / n if component_sizes else 0
    if len(components) > 1:
        severity = "warning" if n >= 5 and largest_ratio < 0.8 else "info"
        findings.append(QualityFinding(
            code="graph.disconnected_components",
            severity=severity,
            message=(
                f"The graph has {len(components)} disconnected components. "
                "That can be fine for independent areas, but mixed-source bundles usually benefit from bridge concepts or typed relations."
            ),
            detail={"component_sizes": component_sizes, "largest_ratio": round(largest_ratio, 3)},
        ))

    orphan_ratio = len(orphan_ids) / n
    if orphan_ids:
        severity = "warning" if (len(orphan_ids) >= 5 or orphan_ratio >= 0.2) and n >= 5 else "info"
        findings.append(QualityFinding(
            code="graph.orphans",
            severity=severity,
            message=(
                f"{len(orphan_ids)} concept(s) have no incoming or outgoing resolved links."
            ),
            concept_ids=[concept_id_to_str(cid) for cid in orphan_ids],
            detail={"count": len(orphan_ids), "ratio": round(orphan_ratio, 3)},
        ))
    return findings


def _normalise_title(title: str) -> str:
    out = []
    last_space = False
    for ch in title.lower():
        if ch.isalnum():
            out.append(ch)
            last_space = False
        elif not last_space:
            out.append(" ")
            last_space = True
    return " ".join("".join(out).split())


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def _non_empty_scalar(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and not isinstance(value, (list, dict))


def _hierarchy_relation_count(frontmatter: dict[str, Any]) -> int:
    count = 0
    rels = frontmatter.get("relations")
    if isinstance(rels, list):
        for rel in rels:
            if not isinstance(rel, dict):
                continue
            rel_type = str(rel.get("type", "") or "").strip().lower()
            if rel_type in HIERARCHY_REL_TYPES:
                count += 1
    for key in ("part_of", "parent", "parent_concept"):
        if _non_empty_scalar(frontmatter.get(key)):
            count += 1
    return count
