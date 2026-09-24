"""Discovery: find gaps in a bundle and emit agent-readable suggestions.

Discovery NEVER silently edits files. It produces a ``DiscoveryReport`` of
``Suggestion`` objects that a human or agent reviews and turns into an
``UpdatePlan`` (see ``okf_loom.update``). This preserves the SPEC §9
permissiveness contract (research.md §C.5: "suggestions ... are never applied
automatically to hand-authored files").

Rules implemented (each a function ``Bundle -> list[Suggestion]``):
    - ``unlinked_mentions``     (core capability ``okf.cap.discovery_mentions``)
    - ``missing_indexes``
    - ``missing_descriptions``
    - ``broken_links``          (re-surfaces validate's broken-link findings)
    - ``missing_relations_hint``(heuristic, conservative)
    - ``orphan_concepts``
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .aliases import discoverable_alias_labels
from .model import Bundle, Concept
from .parse import _INLINE_CODE_RE, _LINK_RE, _strip_code_blocks
from .paths import (
    ConceptId,
    ConceptIdError,
    concept_id_from_str,
    concept_id_to_str,
)
from .validate import CheckSpec, validate_bundle


# Common labels that produce many false positive mention suggestions in
# imported/wiki-like bundles. They are not hard-banned; they lower confidence
# so callers can still request them with include_low_confidence.
_NOISY_MENTION_PHRASES = {
    "admin",
    "ben",
    "client",
    "clients",
    "current",
    "data",
    "document",
    "documents",
    "history",
    "home",
    "index",
    "issue",
    "issues",
    "note",
    "notes",
    "page",
    "people",
    "project",
    "projects",
    "reference",
    "references",
    "user",
    "users",
    "wiki",
    "wiki page",
}

_NOISY_STATUS_PHRASES = {
    "accepted",
    "active",
    "closed",
    "complete",
    "completed",
    "done",
    "inactive",
    "new",
    "open",
    "pending",
    "rejected",
    "resolved",
}

_GENERIC_DUPLICATE_LABELS = {
    "architecture",
    "design",
    "design doc",
    "implementation plan",
    "open questions",
    "overview",
    "product spec",
    "readme",
    "requirements",
    "roadmap",
    "session log",
    "status",
    "summary",
}

_COMMON_PERSON_NAMES = {
    "aaron",
    "adam",
    "alex",
    "andrew",
    "anthony",
    "ben",
    "brian",
    "chris",
    "christopher",
    "dan",
    "daniel",
    "david",
    "emily",
    "eric",
    "george",
    "james",
    "jason",
    "john",
    "joseph",
    "kevin",
    "laura",
    "lisa",
    "mark",
    "matt",
    "matthew",
    "michael",
    "paul",
    "peter",
    "robert",
    "sarah",
    "steve",
    "steven",
    "tom",
    "william",
}

_ALWAYS_SUPPRESS_REASONS = {
    "already_structurally_related",
    "configured_phrase",
    "configured_pair",
}


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class Suggestion:
    """One agent-readable discovery suggestion.

    Attributes:
        rule: the discovery rule that produced this (e.g. ``unlinked_mentions``).
        severity: ``"info"`` or ``"warning"``.
        message: human-readable description.
        concept_id: the concept the suggestion applies to (the *source* for
            relation/link suggestions). None for directory-scoped rules.
        target_concept_id: for relation/link suggestions, the proposed target.
        action: short verb phrase, e.g. ``"add link"``.
        detail: machine-readable fields (rule-specific).
    """

    rule: str
    severity: str
    message: str
    concept_id: ConceptId | None
    target_concept_id: ConceptId | None
    action: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "concept_id": (
                concept_id_to_str(self.concept_id) if self.concept_id else None
            ),
            "target_concept_id": (
                concept_id_to_str(self.target_concept_id)
                if self.target_concept_id
                else None
            ),
            "action": self.action,
            "detail": dict(self.detail),
        }


@dataclass
class DiscoveryReport:
    """Aggregate result of running one or more discovery rules."""

    bundle_root: Path
    suggestions: list[Suggestion] = field(default_factory=list)
    suppressed: list[Suggestion] = field(default_factory=list)

    def by_rule(self) -> dict[str, list[Suggestion]]:
        out: dict[str, list[Suggestion]] = {}
        for s in self.suggestions:
            out.setdefault(s.rule, []).append(s)
        return out

    def as_dict(self) -> dict:
        by_rule = self.by_rule()
        actionability = _actionability_buckets(self.suggestions, self.suppressed)
        return {
            "bundle_root": str(self.bundle_root),
            "total": len(self.suggestions),
            "counts": {rule: len(items) for rule, items in by_rule.items()},
            "suppressed_total": len(self.suppressed),
            "suppressed_counts": {
                rule: len(items)
                for rule, items in _group_suggestions(self.suppressed).items()
            },
            "suppressed_reason_counts": _suppression_reason_counts(
                self.suppressed
            ),
            "actionability_counts": {
                bucket: len(items) for bucket, items in actionability.items()
            },
            "actionability": actionability,
            "suggestions": [s.as_dict() for s in self.suggestions],
            "suppressed": [s.as_dict() for s in self.suppressed],
        }


@dataclass(frozen=True)
class _MentionCandidate:
    concept_id: ConceptId
    sources: frozenset[str]


@dataclass(frozen=True)
class _MentionOccurrence:
    line: int
    location: str


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def discover_suggestions(
    bundle: Bundle,
    *,
    rules: list[str] | None = None,
    scope: list[str] | set[str] | None = None,
    min_confidence: float = 0.5,
    include_low_confidence: bool = False,
) -> DiscoveryReport:
    """Run discovery rules against a loaded bundle.

    Args:
        bundle: a loaded ``Bundle``.
        rules: subset of rule names to run, or None for all rules.
        scope: optional set/list of concept-id strings (slash form, e.g.
            ``"tables/orders"``). When given (current spec §7 scoped enrichment),
            the report keeps ONLY suggestions whose *subject* concept is in
            scope. Targets remain bundle-wide — a scoped concept may still be
            advised to link to any concept. Directory-level rules
            (``missing_indexes``) are dropped when scope is active because they
            carry no subject concept. ``scope=None`` keeps the full report
            (unchanged behaviour).
        min_confidence: default confidence threshold for noisy heuristic rules.
            Currently applies to ``unlinked_mentions`` only.
        include_low_confidence: when True, keep low-confidence suggestions in
            ``suggestions`` instead of moving them to ``suppressed``.

    Returns:
        A ``DiscoveryReport`` whose suggestions are grouped by rule.
    """
    selected = list(rules) if rules is not None else list(_ALL_RULES)
    unknown = [r for r in selected if r not in _ALL_RULES]
    if unknown:
        raise ValueError(f"Unknown discovery rules: {sorted(unknown)}")
    suggestions: list[Suggestion] = []
    for name in selected:
        suggestions.extend(_ALL_RULES[name](bundle))
    suppressed: list[Suggestion] = []
    kept: list[Suggestion] = []
    for s in suggestions:
        if s.rule == "unlinked_mentions":
            suppression_reasons = _detail_list(s.detail, "suppression_reasons")
            if any(r in _ALWAYS_SUPPRESS_REASONS for r in suppression_reasons):
                s.detail["suppression_reasons"] = suppression_reasons
                suppressed.append(s)
                continue
            if not include_low_confidence:
                confidence = float(s.detail.get("confidence", 1.0))
                if confidence < min_confidence:
                    if "low_confidence" not in suppression_reasons:
                        suppression_reasons.append("low_confidence")
                    s.detail["suppression_reasons"] = suppression_reasons
                    suppressed.append(s)
                    continue
        kept.append(s)
    suggestions = kept
    # Current spec §7: scoped enrichment. When a scope set is given, keep only
    # suggestions whose subject concept_id is in scope. The neighbour
    # expansion (§11 ``--neighbors``) happens in plan.build_plan before this
    # call, so discover just filters to whatever set it receives. scope=None
    # leaves the report untouched (preserves all existing behaviour).
    if scope is not None:
        scope_ids: set[ConceptId] = set()
        for s in scope:
            s = s.strip()
            if not s:
                continue
            try:
                scope_ids.add(concept_id_from_str(s))
            except (ConceptIdError, ValueError):
                continue  # ignore an unparseable scope id; never crash.
        suggestions = [
            s for s in suggestions
            if s.concept_id is not None and s.concept_id in scope_ids
        ]
        suppressed = [
            s for s in suppressed
            if s.concept_id is not None and s.concept_id in scope_ids
        ]
    return DiscoveryReport(bundle_root=bundle.root, suggestions=suggestions, suppressed=suppressed)


def _group_suggestions(suggestions: list[Suggestion]) -> dict[str, list[Suggestion]]:
    out: dict[str, list[Suggestion]] = {}
    for s in suggestions:
        out.setdefault(s.rule, []).append(s)
    return out


def _detail_list(detail: dict, key: str) -> list[str]:
    value = detail.get(key)
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _suppression_reason_counts(suggestions: list[Suggestion]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in suggestions:
        for reason in _detail_list(s.detail, "suppression_reasons"):
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _suggestion_bucket(suggestion: Suggestion, *, suppressed: bool) -> str:
    detail = suggestion.detail
    confidence_reasons = set(_detail_list(detail, "confidence_reasons"))
    suppression_reasons = set(_detail_list(detail, "suppression_reasons"))
    if suppressed:
        if "already_structurally_related" in suppression_reasons:
            return "suppressed_existing_relation"
        if {"configured_phrase", "configured_pair"} & suppression_reasons:
            return "suppressed_configured"
        if (
            "different_graph_cluster" in confidence_reasons
            or "different_top_level_folder" in confidence_reasons
            or "cross_context_generic_label" in confidence_reasons
        ):
            return "suppressed_cross_cluster"
        if (
            "generic_duplicate_label" in confidence_reasons
            or "common_label" in confidence_reasons
            or "common_person_name" in confidence_reasons
            or "status_label" in confidence_reasons
            or "high_document_frequency" in confidence_reasons
        ):
            return "suppressed_generic_label"
        if "low_confidence" in suppression_reasons:
            return "low_confidence"
        return "suppressed"

    if suggestion.rule == "missing_indexes":
        return "safe_to_apply"
    if suggestion.rule == "unlinked_mentions":
        confidence = float(detail.get("confidence", 0.0))
        location_counts = detail.get("location_counts")
        has_body = (
            isinstance(location_counts, dict)
            and int(location_counts.get("body", 0) or 0) > 0
        )
        noisy = bool({
            "generic_duplicate_label",
            "common_label",
            "common_person_name",
            "status_label",
            "different_graph_cluster",
            "different_top_level_folder",
            "cross_context_generic_label",
        } & confidence_reasons)
        if confidence >= 0.7 and has_body and not noisy:
            return "safe_to_apply"
    return "needs_review"


def _actionability_buckets(
    suggestions: list[Suggestion],
    suppressed: list[Suggestion],
) -> dict[str, list[dict]]:
    bucket_names = (
        "safe_to_apply",
        "needs_review",
        "suppressed_existing_relation",
        "suppressed_configured",
        "suppressed_generic_label",
        "suppressed_cross_cluster",
        "low_confidence",
        "suppressed",
    )
    buckets: dict[str, list[dict]] = {name: [] for name in bucket_names}
    for suggestion in suggestions:
        bucket = _suggestion_bucket(suggestion, suppressed=False)
        item = suggestion.as_dict()
        item["actionability_bucket"] = bucket
        buckets.setdefault(bucket, []).append(item)
    for suggestion in suppressed:
        bucket = _suggestion_bucket(suggestion, suppressed=True)
        item = suggestion.as_dict()
        item["actionability_bucket"] = bucket
        buckets.setdefault(bucket, []).append(item)
    return {bucket: items for bucket, items in buckets.items() if items}


def _load_discover_config(bundle: Bundle) -> object:
    from .config import DiscoverConfig, OkfConfig, OkfConfigError

    try:
        return OkfConfig.load(bundle.root).discover
    except OkfConfigError:
        return DiscoverConfig()


def _configured_suppress_phrases(cfg: object) -> set[str]:
    return set(getattr(cfg, "suppress_phrases", ()))


def _configured_suppress_pairs(cfg: object) -> set[tuple[ConceptId, ConceptId]]:
    return {
        (pair.source, pair.target)
        for pair in getattr(cfg, "suppress_pairs", ())
    }


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------

_ALL_RULES: dict[str, Callable[[Bundle], list[Suggestion]]] = {}


def _rule(name: str) -> Callable:
    def deco(fn: Callable[[Bundle], list[Suggestion]]) -> Callable:
        _ALL_RULES[name] = fn
        return fn

    return deco


# ---------------------------------------------------------------------------
# unlinked_mentions  (okf.cap.discovery_mentions)
# ---------------------------------------------------------------------------


def _scannable_body(body: str) -> str:
    """Return a copy of ``body`` safe for mention scanning.

    Fenced code blocks, inline code spans, and existing markdown links are
    blanked out while preserving line counts so line numbers stay accurate.
    """
    cleaned = _strip_code_blocks(body)  # fences -> blank lines (line-safe)
    # Blank inline-code *content* but keep a backtick marker (line-safe).
    cleaned = _INLINE_CODE_RE.sub(
        lambda m: "`" + "\n" * m.group(0).count("\n"), cleaned
    )
    # Blank existing markdown link spans (line-safe).
    cleaned = _LINK_RE.sub(lambda m: "\n" * m.group(0).count("\n"), cleaned)
    return cleaned


def _mention_location(body_lines: list[str], line_no: int) -> str:
    if line_no < 1 or line_no > len(body_lines):
        return "body"
    line = body_lines[line_no - 1].strip()
    if re.match(r"^#\s+\S", line):
        return "h1"
    if re.match(r"^#{2,6}\s+\S", line):
        return "heading"
    if line.startswith("|") and line.count("|") >= 2:
        return "table"
    return "body"


def _scannable_frontmatter(concept: Concept) -> str:
    parts: list[str] = []
    for key in ("title", "description"):
        value = concept.frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def _location_counts(occurrences: list[_MentionOccurrence]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for occurrence in occurrences:
        counts[occurrence.location] = counts.get(occurrence.location, 0) + 1
    return counts


def _build_title_index(bundle: Bundle) -> dict[str, _MentionCandidate]:
    """Map a lowercased mention phrase to a single concept id.

    Only phrases that uniquely identify ONE concept are kept; ambiguous phrases
    (shared by multiple concepts) are dropped to avoid bad suggestions. Short
    (< 3 chars) or non-alphanumeric phrases are dropped as too noisy.
    """
    phrase_to_sources: dict[str, dict[ConceptId, set[str]]] = {}
    for c in bundle.concepts.values():
        phrases: dict[str, set[str]] = {}
        title = (c.title or "").strip().lower()
        if title:
            phrases.setdefault(title, set()).add("title")
        last_seg = (c.id[-1] if c.id else "").strip().lower()
        if last_seg:
            phrases.setdefault(last_seg, set()).add("id_segment")
        for alias in discoverable_alias_labels(c.frontmatter.get("aliases")):
            phrases.setdefault(alias.lower(), set()).add("alias")
        for p, sources in phrases.items():
            if len(p) < 3 or not re.search(r"[a-z0-9]", p):
                continue
            phrase_to_sources.setdefault(p, {}).setdefault(c.id, set()).update(sources)
    out: dict[str, _MentionCandidate] = {}
    for phrase, by_cid in phrase_to_sources.items():
        if len(by_cid) != 1:
            continue
        cid, sources = next(iter(by_cid.items()))
        out[phrase] = _MentionCandidate(concept_id=cid, sources=frozenset(sources))
    return out


def _structural_relation_targets(concept: Concept) -> set[ConceptId]:
    targets: set[ConceptId] = set()
    rels = concept.frontmatter.get("relations")
    if not isinstance(rels, list):
        return targets
    for rel in rels:
        if not isinstance(rel, dict):
            continue
        target_raw = rel.get("target")
        if not isinstance(target_raw, str) or not target_raw.strip():
            continue
        try:
            targets.add(concept_id_from_str(target_raw))
        except (ConceptIdError, ValueError):
            continue
    return targets


def _top_level_folder(cid: ConceptId) -> str:
    return cid[0] if len(cid) > 1 else ""


def _parent_folder(cid: ConceptId) -> ConceptId:
    return cid[:-1] if len(cid) > 1 else ()


def _mention_confidence(
    phrase: str,
    candidate: _MentionCandidate,
    occurrences: list[_MentionOccurrence],
    bundle: Bundle,
    source: Concept,
    document_frequency: int,
) -> tuple[float, list[str]]:
    score = 0.35
    reasons: list[str] = []
    if "title" in candidate.sources:
        score += 0.25
        reasons.append("title_match")
    if "alias" in candidate.sources:
        score += 0.20
        reasons.append("alias_match")
    if candidate.sources == frozenset({"id_segment"}):
        score += 0.05
        reasons.append("id_segment_only")
    if " " in phrase or "-" in phrase or "_" in phrase:
        score += 0.15
        reasons.append("multiword")
    if len(phrase) >= 8:
        score += 0.10
        reasons.append("long_phrase")
    if len(phrase) >= 16:
        score += 0.05
        reasons.append("very_long_phrase")
    if len(occurrences) >= 2:
        score += 0.05
        reasons.append("repeated")
    location_counts = _location_counts(occurrences)
    has_body_prose = location_counts.get("body", 0) > 0
    if has_body_prose and len(location_counts) > 1:
        score += 0.05
        reasons.append("also_mentioned_in_body")
    if not has_body_prose:
        if set(location_counts) == {"frontmatter"}:
            score -= 0.35
            reasons.append("frontmatter_only")
        elif location_counts.get("h1", 0) and len(location_counts) == 1:
            score -= 0.35
            reasons.append("h1_only")
        elif (
            location_counts.get("heading", 0)
            and location_counts.get("table", 0) == 0
        ):
            score -= 0.25
            reasons.append("heading_only")
        elif location_counts.get("table", 0):
            score -= 0.20
            reasons.append("table_only")
        else:
            score -= 0.20
            reasons.append("non_body_only")
    concept_count = max(1, len(bundle.concepts))
    doc_ratio = document_frequency / concept_count
    if document_frequency >= 10 or (concept_count >= 20 and doc_ratio >= 0.08):
        score -= 0.35
        reasons.append("high_document_frequency")
    elif document_frequency >= 4 and (concept_count >= 20 and doc_ratio >= 0.05):
        score -= 0.15
        reasons.append("moderate_document_frequency")
    if phrase in _COMMON_PERSON_NAMES:
        score -= 0.40
        reasons.append("common_person_name")
    if phrase in _NOISY_STATUS_PHRASES:
        score -= 0.40
        reasons.append("status_label")
    target = bundle.concepts.get(candidate.concept_id)
    target_type = str((target.type if target else "") or "").strip().lower()
    if target_type in {"document", "wiki page", "index", "page"}:
        score -= 0.15
        reasons.append("generic_target_type")
    else:
        score += 0.05
        reasons.append("specific_target_type")
    if phrase in _NOISY_MENTION_PHRASES:
        score -= 0.45
        reasons.append("common_label")
    # Source-aware nudge: matching source/cluster metadata slightly strengthens
    # a suggestion; cross-source suggestions need stronger textual evidence.
    source_system = str(source.frontmatter.get("source_system") or "").strip()
    source_cluster = str(source.frontmatter.get("graph_cluster") or "").strip()
    target_system = ""
    target_cluster = ""
    if target is not None:
        target_system = str(target.frontmatter.get("source_system") or "").strip()
        target_cluster = str(target.frontmatter.get("graph_cluster") or "").strip()
    strong_context = False
    cross_context = False
    if source_system and target_system:
        if source_system == target_system:
            score += 0.04
            reasons.append("same_source_system")
        else:
            score -= 0.05
            reasons.append("different_source_system")
            cross_context = True
    if source_cluster and target_cluster:
        if source_cluster == target_cluster:
            score += 0.10
            reasons.append("same_graph_cluster")
            strong_context = True
        else:
            score -= 0.08
            reasons.append("different_graph_cluster")
            cross_context = True
    if target is not None:
        source_parent = _parent_folder(source.id)
        target_parent = _parent_folder(target.id)
        if source_parent and target_parent and source_parent == target_parent:
            score += 0.06
            reasons.append("same_parent_folder")
            strong_context = True
        source_top = _top_level_folder(source.id)
        target_top = _top_level_folder(target.id)
        if source_top and target_top:
            if source_top == target_top:
                score += 0.04
                reasons.append("same_top_level_folder")
                strong_context = True
            else:
                score -= 0.04
                reasons.append("different_top_level_folder")
                cross_context = True
    if phrase in _GENERIC_DUPLICATE_LABELS:
        reasons.append("generic_duplicate_label")
        if strong_context:
            score -= 0.15
            reasons.append("generic_duplicate_label_same_context")
        else:
            score -= 0.50
            reasons.append("generic_duplicate_label_weak_context")
        if cross_context:
            score -= 0.10
            reasons.append("cross_context_generic_label")
    score = max(0.0, min(1.0, score))
    return round(score, 3), reasons


@_rule("unlinked_mentions")
def _rule_unlinked_mentions(bundle: Bundle) -> list[Suggestion]:
    out: list[Suggestion] = []
    title_index = _build_title_index(bundle)
    if not title_index:
        return out
    discover_config = _load_discover_config(bundle)
    configured_phrases = _configured_suppress_phrases(discover_config)
    configured_pairs = _configured_suppress_pairs(discover_config)
    compiled = {
        p: re.compile(rf"\b{re.escape(p)}\b", re.IGNORECASE)
        for p in title_index
    }
    scannables = {
        src.id: _scannable_body(src.body)
        for src in sorted(bundle.concepts.values(), key=lambda c: c.id)
    }
    frontmatter_scannables = {
        src.id: _scannable_frontmatter(src)
        for src in sorted(bundle.concepts.values(), key=lambda c: c.id)
    }
    document_frequency: dict[str, int] = {}
    for phrase, pat in compiled.items():
        document_frequency[phrase] = sum(
            1
            for src_id, text in scannables.items()
            if pat.search(text) or pat.search(frontmatter_scannables[src_id])
        )
    for src in sorted(bundle.concepts.values(), key=lambda c: c.id):
        scannable = scannables[src.id]
        frontmatter_scannable = frontmatter_scannables[src.id]
        if not scannable.strip() and not frontmatter_scannable.strip():
            continue
        already_linked = {
            link.target
            for link in src.links(bundle_root=bundle.root)
            if link.target is not None
        }
        relation_targets = _structural_relation_targets(src)
        for phrase, candidate in title_index.items():
            target_cid = candidate.concept_id
            if target_cid == src.id:
                continue  # skip self-mentions
            if target_cid in already_linked:
                continue  # source already links to target
            pat = compiled[phrase]
            body_lines = src.body.splitlines()
            occurrences = []
            for m in pat.finditer(frontmatter_scannable):
                occurrences.append(
                    _MentionOccurrence(line=0, location="frontmatter")
                )
            for m in pat.finditer(scannable):
                line_no = scannable.count("\n", 0, m.start()) + 1
                occurrences.append(
                    _MentionOccurrence(
                        line=line_no,
                        location=_mention_location(body_lines, line_no),
                    )
                )
            if not occurrences:
                continue
            target = bundle.concepts.get(target_cid)
            target_title = target.title if target else phrase
            confidence, reasons = _mention_confidence(
                phrase,
                candidate,
                occurrences,
                bundle,
                src,
                document_frequency.get(phrase, 0),
            )
            suppression_reasons: list[str] = []
            if target_cid in relation_targets:
                suppression_reasons.append("already_structurally_related")
            if phrase in configured_phrases:
                suppression_reasons.append("configured_phrase")
            if (src.id, target_cid) in configured_pairs:
                suppression_reasons.append("configured_pair")
            if suppression_reasons:
                message = (
                    f"{src.title!r} mentions {target_title!r} "
                    f"({len(occurrences)}x) but already has a "
                    "structured relation to it."
                )
                action = "no action"
            else:
                message = (
                    f"{src.title!r} mentions {target_title!r} "
                    f"({len(occurrences)}x) but does not link to it."
                )
                action = "add link"
            out.append(
                Suggestion(
                    rule="unlinked_mentions",
                    severity="info",
                    message=message,
                    concept_id=src.id,
                    target_concept_id=target_cid,
                    action=action,
                    detail={
                        "label": target_title,
                        "phrase": phrase,
                        "confidence": confidence,
                        "confidence_reasons": reasons,
                        "suppression_reasons": suppression_reasons,
                        "document_frequency": document_frequency.get(phrase, 0),
                        "location_counts": _location_counts(occurrences),
                        "suggested_target_concept_id": concept_id_to_str(
                            target_cid
                        ),
                        "occurrences": [o.line for o in occurrences],
                        "occurrence_locations": [
                            {"line": o.line, "location": o.location}
                            for o in occurrences
                        ],
                    },
                )
            )
    return out


# ---------------------------------------------------------------------------
# missing_indexes
# ---------------------------------------------------------------------------


@_rule("missing_indexes")
def _rule_missing_indexes(bundle: Bundle) -> list[Suggestion]:
    out: list[Suggestion] = []
    dir_has_index = {
        idx.rel_path.parent for idx in bundle.indexes.values()
    }
    concept_dirs = {c.rel_path.parent for c in bundle.concepts.values()}
    for d in sorted(concept_dirs):
        if d in dir_has_index:
            continue
        out.append(
            Suggestion(
                rule="missing_indexes",
                severity="warning",
                message=(
                    f"Directory {d}/ contains concepts but has no index.md "
                    "for progressive disclosure (upstream OKF 0.2 §8)."
                ),
                concept_id=None,
                target_concept_id=None,
                action="create index.md",
                detail={"directory": str(d)},
            )
        )
    return out


# ---------------------------------------------------------------------------
# missing_descriptions
# ---------------------------------------------------------------------------


@_rule("missing_descriptions")
def _rule_missing_descriptions(bundle: Bundle) -> list[Suggestion]:
    out: list[Suggestion] = []
    for c in sorted(bundle.concepts.values(), key=lambda c: c.id):
        if c.description and c.description.strip():
            continue
        out.append(
            Suggestion(
                rule="missing_descriptions",
                severity="warning",
                message=(
                    f"Concept {c.title!r} has no description frontmatter."
                ),
                concept_id=c.id,
                target_concept_id=None,
                action="add description",
                detail={},
            )
        )
    return out


# ---------------------------------------------------------------------------
# broken_links  (re-surfaces validate findings)
# ---------------------------------------------------------------------------


@_rule("broken_links")
def _rule_broken_links(bundle: Bundle) -> list[Suggestion]:
    out: list[Suggestion] = []
    report = validate_bundle(bundle, checks=CheckSpec.only("link_integrity"))
    for f in report.findings:
        if f.code != "link.broken":
            continue
        detail = dict(f.detail or {})
        out.append(
            Suggestion(
                rule="broken_links",
                severity="warning",
                message=f.message,
                concept_id=f.concept_id,
                target_concept_id=None,
                action="fix or remove link",
                detail={
                    "target_raw": detail.get("target_raw"),
                    "form": detail.get("form"),
                    "label": detail.get("label"),
                    "line": f.line,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# missing_relations_hint  (heuristic, conservative)
# ---------------------------------------------------------------------------

# Unambiguous FK signals. "references" is required to be followed by something
# table-like to avoid matching the noun "reference" / "See the references".
_FK_PATTERNS = [
    re.compile(r"\bFK\s+to\s+\S", re.IGNORECASE),
    re.compile(r"\bforeign\s+key\b", re.IGNORECASE),
    re.compile(r"\breferenced?\s+(?:to\s+)?[A-Za-z_][\w]*", re.IGNORECASE),
]


@_rule("missing_relations_hint")
def _rule_missing_relations_hint(bundle: Bundle) -> list[Suggestion]:
    out: list[Suggestion] = []
    graph = bundle.graph()
    for c in sorted(bundle.concepts.values(), key=lambda c: c.id):
        schema = c.schema_section
        if not schema:
            continue
        # Conservative: only hint when the concept has NO resolved outgoing
        # internal edges (the FK relationship is entirely unrepresented).
        # Broken links (target=None / not in bundle) do NOT count as a
        # represented relationship.
        resolved_out = [
            l for l in graph.out_edges.get(c.id, []) if l.target in graph.concept_ids
        ]
        if resolved_out:
            continue
        matched: list[str] = []
        for line in schema.splitlines():
            if any(pat.search(line) for pat in _FK_PATTERNS):
                matched.append(line.strip())
        if not matched:
            continue
        out.append(
            Suggestion(
                rule="missing_relations_hint",
                severity="info",
                message=(
                    f"{c.title!r} schema references a foreign-key-like column "
                    f"but the concept has no outgoing relation link."
                ),
                concept_id=c.id,
                target_concept_id=None,
                action="add relation link",
                detail={
                    "matched_text": matched[0],
                    "matched_lines": matched,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# orphan_concepts
# ---------------------------------------------------------------------------


@_rule("orphan_concepts")
def _rule_orphan_concepts(bundle: Bundle) -> list[Suggestion]:
    out: list[Suggestion] = []
    graph = bundle.graph()
    for cid in sorted(graph.concept_ids):
        if graph.in_edges.get(cid) or graph.out_edges.get(cid):
            continue
        concept = bundle.concepts.get(cid)
        title = concept.title if concept else concept_id_to_str(cid)
        out.append(
            Suggestion(
                rule="orphan_concepts",
                severity="info",
                message=(
                    f"{title!r} has no incoming or outgoing internal links "
                    "(orphan node in the graph)."
                ),
                concept_id=cid,
                target_concept_id=None,
                action="connect",
                detail={},
            )
        )
    return out
