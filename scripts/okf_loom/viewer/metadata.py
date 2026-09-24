"""Escaped, read-only OKF metadata presentation shared by every viewer mode.

Semantics belong to okf_loom.metadata. This adapter never dereferences resources
or interprets computation fields as executable content.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape, unescape
import hashlib
import re

from ..metadata import parse_instant, metadata_issues
from .markdown import url_for_concept


FIELDS = frozenset({"generated", "timestamp", "verified", "sources", "usage_window",
                    "status", "stale_after", "runtime", "parameters", "computation",
                    "executor", "attester", "receipt"})


def text(value):
    if value is None:
        return "Not recorded"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return escape(str(value), quote=True)


def source_anchor(concept, source_id):
    identity = "/".join(concept.id) + "\0" + source_id
    return "okf-source-" + hashlib.sha256(identity.encode()).hexdigest()


def source_ids(concept):
    rows = concept.metadata.get("sources", [])
    return Counter(row["id"] for row in rows if isinstance(row, dict)
                   and isinstance(row.get("id"), str) and row["id"].strip()) if isinstance(rows, list) else Counter()


def join_source_footnotes(body_html, concept):
    """Join exact authored ids, never the source list position or slug.

    Only href/name attributes change; canonical visible text and projection
    offsets remain untouched. Ordinary footnotes and Loom citations stay intact.
    Ambiguous duplicate source ids deliberately do not acquire an attribution.
    """
    ids = source_ids(concept)
    def join(match):
        fid = unescape(match[2])
        if ids[fid] != 1:
            return match[0]
        return (match[1] + 'href="#' + source_anchor(concept, fid)
                + '" data-okf-source-ref="' + text(fid)
                + '" aria-label="Source ' + text(fid) + '">[' + match[2] + ']</a>')
    return re.sub(r'(<sup class="okf-footnote-ref"[^>]*><a )href="[^"]*">\[([^<]*)\]</a>', join, body_html)


def _value(value):
    """Structured human-readable values, not a JSON/config pane."""
    if isinstance(value, dict):
        return '<dl class="okf-metadata__facts">' + ''.join(
            '<dt>' + text(str(k).replace('_', ' ').capitalize()) + '</dt><dd>'
            + _value(v) + '</dd>' for k, v in value.items()) + '</dl>'
    if isinstance(value, list):
        return '<ul>' + ''.join('<li>' + _value(v) + '</li>' for v in value) + '</ul>' if value else 'None recorded'
    return text(value)


# Search-result advisory group (U4). The concept page keeps its full badges;
# a result lists only states that differ from the OKF defaults, so an ordinary
# "unverified / stable / no expiry" concept adds nothing to its meta line.
# portable-retrieval.js ``advisoryTokens`` mirrors these rules and wording.
SEARCH_TRUST_SHOWN = ("machine-confirmed", "human-reviewed")
SEARCH_LIFECYCLE_SHOWN = ("draft", "deprecated")
SEARCH_LIFECYCLE_PREVIEW = 40


def search_advisory_tokens(metadata, now):
    """Return ``[(kind, text, attention)]`` for non-default advisory states.

    ``metadata`` is the bounded public projection a search result carries;
    absent keys mean their OKF default. ``now`` is an aware datetime taken
    once per rendered result page. Lifecycle and freshness tokens need the
    reader's attention; trust is informational.
    """
    if not isinstance(metadata, dict) or not metadata:
        return []
    tokens = []
    trust = metadata.get("trust_tier", "unverified")
    if isinstance(trust, str) and trust in SEARCH_TRUST_SHOWN:
        tokens.append(("trust", "Trust: " + trust, False))
    if "status" in metadata:
        status = metadata["status"]
        if not isinstance(status, str):
            tokens.append(("lifecycle", "Lifecycle: invalid value", True))
        elif status in SEARCH_LIFECYCLE_SHOWN:
            tokens.append(("lifecycle", "Lifecycle: " + status, True))
        elif status != "stable":
            shown = status[:SEARCH_LIFECYCLE_PREVIEW]
            if len(status) > SEARCH_LIFECYCLE_PREVIEW:
                shown += "…"
            tokens.append(("lifecycle", "Lifecycle: " + shown + " (unrecognized)", True))
    if "stale_after" in metadata:
        expiry = parse_instant(metadata["stale_after"])
        if expiry is None:
            tokens.append(("freshness", "Freshness unknown (invalid expiry)", True))
        elif now >= expiry:
            tokens.append(("freshness", "Stale", True))
    return tokens


def render_search_advisory(metadata, now):
    """Inline advisory group appended to a search result's meta line, or ``""``.

    Plain escaped words only: no icons, tooltips or aria-label overrides, and
    no ``data-okf-stale-at`` (the concept page's live clock must not rewrite a
    result evaluated when the page rendered).
    """
    tokens = search_advisory_tokens(metadata, now)
    if not tokens:
        return ""
    return ('<span class="okf-search-advisory"> · Advisory' + ''.join(
        ' · <span data-okf-advisory="' + kind + '"'
        + (' data-okf-advisory-attention' if attention else '') + '>'
        + text(label) + '</span>' for kind, label, attention in tokens) + '</span>')


def render_metadata(concept, bundle, *, mode="serve", now=None):
    m = concept.metadata
    now = now or datetime.now(timezone.utc)
    expiry = parse_instant(m.get("stale_after"))
    stale = concept.is_stale(now)
    badges = [('Trust (advisory)', m['trust_tier']), ('Lifecycle (advisory)', m['status'])]
    badge_html = ''.join('<span class="okf-metadata__badge">' + text(k) + ': <strong>' + text(v) + '</strong></span>' for k, v in badges)
    freshness = ('Stale' if stale else 'Not stale') if stale is not None else ('Invalid expiry — freshness unknown' if 'stale_after' in m else 'No expiry recorded')
    expiry_attr = f' data-okf-stale-at="{expiry.timestamp() * 1000:.3f}"' if expiry else ''
    badge_html += '<span class="okf-metadata__badge" data-okf-freshness' + expiry_attr + '>' + freshness + '</span>'
    rows = []
    def fact(label, value):
        rows.append('<dt>' + text(label) + '</dt><dd>' + value + '</dd>')
    fact('Generated at' if m.get('generated_at_source') != 'timestamp' else 'Legacy timestamp (generation fallback)', text(m.get('generated_at')))
    if 'timestamp' in concept.frontmatter and m.get('generated_at_source') != 'timestamp':
        fact('Legacy timestamp (not generation time)', text(concept.frontmatter['timestamp']))
    if 'generated' in m:
        fact('Generation', _value(m['generated']))
    if 'verified' in m:
        fact('Verification records (advisory)', _value(m['verified']))
    if 'stale_after' in m:
        fact('Stale on or after', text(m['stale_after']))
    if 'usage_window' in m:
        fact('Shared usage window', _value(m['usage_window']))
    sources = m.get('sources', [])
    source_rows = []
    ids = source_ids(concept)
    links = concept.source_links(bundle_root=bundle.root, concept_ids=set(bundle.concepts))
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict):
            source_rows.append('<li>Invalid source record</li>')
            continue
        sid = source.get('id')
        unique = isinstance(sid, str) and ids[sid] == 1
        anchor = ' id="' + source_anchor(concept, sid) + '" tabindex="-1"' if unique else ''
        resource = source.get('resource')
        title = source.get('title') or resource or 'Unnamed source'
        href = None
        target_attr = ''
        # URL allowlist is intentionally narrower than Markdown: scope names,
        # javascript/data/file URLs, protocol-relative URLs are never actions.
        if isinstance(resource, str) and re.fullmatch(r'https?://[^\s<>\\]+', resource, re.I):
            from urllib.parse import urlsplit
            try:
                if urlsplit(resource).hostname:
                    href = resource
            except ValueError:
                pass
        else:
            link = next((l for l in links if l.target_raw == resource and l.target in bundle.concepts), None)
            if link:
                if mode == 'graph':
                    href = '#node-' + '/'.join(link.target)
                    target_attr = ' data-okf-source-concept="' + text('/'.join(link.target)) + '"'
                    if link.anchor:
                        target_attr += ' data-okf-source-fragment="' + text(link.anchor) + '"'
                else:
                    href = url_for_concept(link.target, mode, source_cid=concept.id)
                    if link.anchor:
                        from urllib.parse import quote
                        href += '#' + quote(link.anchor, safe='')
        title_html = ('<a href="' + text(href) + '"' + target_attr + ' rel="noopener noreferrer">' + text(title) + '</a>') if href else '<span>' + text(title) + '</span>'
        identity = ('Source ' + text(sid)) if sid else 'Source without id'
        if sid and not unique:
            identity += ' — duplicate id; attribution unavailable'
        fields = ''.join('<dt>' + text(label) + '</dt><dd>' + _value(source[key]) + '</dd>' for key, label in (
            ('resource', 'Resource / scope'), ('author', 'Author'), ('last_modified', 'Last modified'),
            ('usage_count', 'Usage count (not credibility)'), ('usage_window', 'Effective usage window')) if key in source)
        source_rows.append('<li' + anchor + '><strong>' + identity + '</strong> · ' + title_html + '<dl class="okf-metadata__facts">' + fields + '</dl></li>')
    sources_html = '<h3>Sources</h3><p>Source ids join claim footnotes; these are separate from Loom citations. Usage is not a credibility score.</p>'
    if not isinstance(sources, list):
        sources_html += '<p>Sources metadata is malformed; no attribution is available.</p>'
    else:
        sources_html += '<ul class="okf-metadata__sources">' + ''.join(source_rows) + '</ul>' if source_rows else '<p>No sources recorded.</p>'
    computation = {k: m[k] for k in ('runtime', 'parameters', 'computation', 'executor', 'attester', 'receipt') if k in m}
    contract = ('<h3>Computation contract</h3><p>Metadata only — never executed. No executor, attester, or receipt has been checked by this viewer.</p>' + _value(computation)) if computation else ''
    issues = metadata_issues(concept.frontmatter)
    warnings = ('<details class="okf-metadata__diagnostics"><summary>Advanced metadata diagnostics</summary><ul>' + ''.join(
        '<li><strong>' + text(field) + '</strong>: ' + text(guidance) + '</li>'
        for _, field, guidance in issues) + '</ul></details>') if issues else ''
    return ('<section class="okf-metadata" aria-label="Trust, lifecycle and sources">'
            '<div class="okf-metadata__badges">' + badge_html + '</div>'
            '<details class="okf-metadata__details"><summary>Generation, sources and contract</summary>'
            '<p class="okf-metadata__advisory">Advisory metadata, not authorization or proof of correctness.</p>'
            + '<h3>Generation and lifecycle</h3><dl class="okf-metadata__facts">' + ''.join(rows) + '</dl>' + sources_html + contract + warnings + '</details></section>')
