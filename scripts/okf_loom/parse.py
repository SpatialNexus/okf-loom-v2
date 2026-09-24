"""Markdown + YAML frontmatter parsing for OKF documents.

Implements:
    - Frontmatter parsing that preserves key order and unknown keys round-trip.
    - Round-trip-safe serialization (matching upstream OKF semantics).
    - Link extraction that correctly handles BOTH absolute bundle-relative
      links (``/foo/bar.md``, SPEC §5.1, the RECOMMENDED form) AND relative
      links (``./bar.md``, ``../baz/bar.md`` — SPEC §5.2). The upstream
      reference viewer silently skipped absolute links, producing incomplete
      graph edges; this module fixes that.
    - A `strip_markdown_for_search` helper used by the lexical search backend.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Iterator

from . import _yaml_compat as yaml

from .exceptions import OKFParseError
from .paths import ConceptId, concept_id_from_str, ConceptIdError

FRONTMATTER_DELIM = "---"


# --- YAML alias-bomb / billion-laughs DoS defence (P2-1, SPEC §3.1) ---------
#
# ``yaml.safe_load`` enforces no size or node cap. A few hundred bytes of
# frontmatter using YAML aliases can expand exponentially when the parsed
# value is later serialized (json.dumps / yaml.safe_dump into the embedded
# bundle JSON during render), pinning every ThreadingHTTPServer worker.
# Measured baseline: a 386-byte, 7-level / fanout-9 alias bomb constructs
# in ~1.5 ms but serializes to ~32 MB of JSON.
#
# IMPORTANT PyYAML internals note: PyYAML *shares* alias node references at
# compose and construct time — the compact in-memory graph for the bomb
# above is ~70 nodes, and construct_object is called only ~70 times. A node
# counter on compose_node/construct_object therefore does NOT see acyclic
# alias fanout. We use TWO complementary controls:
#
#   1. ``_NodeLimitedSafeLoader`` — a SafeLoader subclass that caps the
#      number of *composed* nodes. This catches NON-ALIAS depth bombs
#      (deeply nested maps/seqs), recursion-stack bombs, and merge-key
#      (``<<``) expansion, and backstops any future PyYAML change to
#      deep-compose aliases.
#
#   2. ``_assert_materialized_under_limit`` — a post-parse walk that counts
#      the object exactly as a serializer will emit it (expanding shared
#      but acyclic subtrees in full, cycle-safe), rejecting if the
#      materialized size exceeds ``MAX_NODES``. This is what actually stops
#      the demonstrated acyclic alias bomb under current PyYAML.

# Real OKF frontmatter is <2 KiB; 64 KiB is a generous ceiling. Anything
# larger is almost certainly an attempted resource-exhaustion payload.
MAX_FRONTMATTER_BYTES = 64 * 1024
# Cap on both composed nodes and materialized (serialized) nodes. 10_000 is
# ~3 orders of magnitude above any legitimate frontmatter/config graph and
# well below any size that could pin a worker.
MAX_NODES = 10_000


class _NodeBudgetExceeded(yaml.YAMLError):
    """Raised when a YAML document constructs or materializes beyond
    ``MAX_NODES``. Subclasses ``yaml.YAMLError`` so the existing
    ``except yaml.YAMLError`` handlers wrap it into ``OKFParseError`` /
    ``OkfConfigError`` automatically."""


class _NodeLimitedSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that caps the number of composed nodes (control 1).

    Defence against non-alias depth/recursion bombs and merge-key expansion.
    Acyclic alias fanout (classic billion-laughs shape) is NOT caught here
    because PyYAML shares alias node references at compose time; that vector
    is handled by ``_assert_materialized_under_limit``.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        node = super().compose_node(parent, index)
        self._okf_node_count = getattr(self, "_okf_node_count", 0) + 1
        if self._okf_node_count > MAX_NODES:
            raise _NodeBudgetExceeded(
                f"YAML document exceeds the {MAX_NODES}-node compose limit"
            )
        return node


def _soft_timestamp(loader: Any, node: Any) -> Any:
    """Keep malformed calendar/offset scalars consumable for soft validation.

    PyYAML raises ValueError on impossible calendars and silently normalizes
    invalid offset minutes. Neither should change conformance or fabricate an
    instant relative to the built-in YAML reader.
    """
    from datetime import datetime
    from .metadata import parse_instant

    try:
        value = loader.construct_yaml_timestamp(node)
    except ValueError:
        return node.value
    if isinstance(value, datetime):
        if parse_instant(node.value) is None:
            return node.value
    return value


if hasattr(_NodeLimitedSafeLoader, "add_constructor"):
    _NodeLimitedSafeLoader.add_constructor("tag:yaml.org,2002:timestamp", _soft_timestamp)


def safe_load_limited(stream: Any) -> Any:
    """``yaml.safe_load`` via the node-counting loader (control 1).

    Drop-in for ``yaml.safe_load`` with the same safe-construction semantics
    (subclass of ``SafeLoader``), plus a composed-node cap.
    """
    return yaml.load(stream, _NodeLimitedSafeLoader)


def _assert_materialized_under_limit(obj: Any, *, limit: int = MAX_NODES) -> None:
    """Reject if the materialized (serialized) form of ``obj`` exceeds
    ``limit`` nodes (control 2).

    Counts nodes exactly as ``json.dumps`` / ``yaml.safe_dump`` would emit
    them: shared-but-acyclic subtrees are counted in full each time they are
    referenced. This is what stops the billion-laughs alias bomb under
    PyYAML, which keeps the in-memory alias graph compact via shared
    references while still exploding at serialization time. Cycles are
    broken via a recursion-path id set so self-referential aliases cannot
    loop forever (cyclic structures are additionally rejected by
    ``json.dumps`` itself, so they are not a size-amplification vector).
    """
    count = 0

    def visit(o: Any, on_path: set[int]) -> None:
        nonlocal count
        # Cycle back-edge: do not charge, do not recurse (prevents infinite
        # loops on self-referential aliases like ``a: &a [*a]``).
        if isinstance(o, (dict, list, tuple)) and id(o) in on_path:
            return
        count += 1
        if count > limit:
            raise _NodeBudgetExceeded(
                f"YAML document materializes to more than {limit} nodes"
            )
        if isinstance(o, dict):
            on_path.add(id(o))
            try:
                for v in o.values():
                    visit(v, on_path)
            finally:
                on_path.discard(id(o))
        elif isinstance(o, (list, tuple)):
            on_path.add(id(o))
            try:
                for item in o:
                    visit(item, on_path)
            finally:
                on_path.discard(id(o))

    try:
        visit(obj, set())
    except RecursionError as e:
        # Pathological nesting depth (real frontmatter nests <20 deep). A
        # structure deep enough to overflow Python's stack is a bomb.
        raise _NodeBudgetExceeded(
            "YAML document nesting exceeds the Python recursion limit"
        ) from e

# A markdown link: [label](target) optionally with a quoted title.
# We are deliberately permissive on the label (allows inline code, etc.) and
# capture the target verbatim. We do not match across newlines.
_LINK_RE = re.compile(
    r"""
    (?<!!)            # not an image (no leading !)
    \[
        (?P<label>[^\]]*)
    \]
    \(
        (?P<target>[^)\s]+)
        (?:\s+"[^"]*")?
    \)
    """,
    re.VERBOSE,
)

# A markdown image: ``![alt](target)`` optionally with a quoted title. Same
# permissive shape as ``_LINK_RE`` minus its ``(?<!!)`` guard — images are
# deliberately NOT graph edges, but their targets still name files, so the
# validator checks them separately (asset.missing).
_IMAGE_LINK_RE = re.compile(
    r"""
    !\[
        (?P<alt>[^\]]*)
    \]
    \(
        (?P<target>[^)\s]+)
        (?:\s+"[^"]*")?
    \)
    """,
    re.VERBOSE,
)

# A wikilink (SPEC §10): ``[[target]]`` or ``[[target|Label]]``. This is body
# syntax (not a frontmatter key). The target is a concept id written
# slash-separated without the ``.md`` suffix (e.g. ``tables/users``). We
# resolve it to a ConceptId with the same primitive the markdown resolver
# uses (``concept_id_from_str``); targets that do not parse are kept with
# ``concept_id=None`` (flagged, not fatal). Wikilinks are emitted with
# ``form="wikilink"``.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

# Fenced code blocks (``` or ~~~) — content inside is not scanned for links.
_FENCE_RE = re.compile(r"^[ ]{0,3}(?P<fence>```+|~~~+)[^\n]*$", re.MULTILINE)

# Inline code spans — also excluded from link scanning.
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

LinkForm = Literal["absolute", "relative", "external", "external_out_of_bundle", "anchor", "wikilink"]


@dataclass(frozen=True)
class ExtractedLink:
    """A markdown link extracted from a document body.

    Attributes:
        target_raw: the link target exactly as written (e.g. ``/a/b.md``,
            ``./c.md``, ``https://x``, ``#section``).
        label: the link text.
        form: link classification.
        anchor: the ``#fragment`` if present, else None.
        line: 1-based line number in the source body where the link appears.
        concept_id: resolved concept id for internal links (absolute/relative
            resolved against the source document's directory). None for
            external, anchor-only, malformed, or unresolved-but-internal links
            (the caller can re-resolve later against the bundle if needed).
    """

    target_raw: str
    label: str
    form: LinkForm
    anchor: str | None
    line: int
    concept_id: ConceptId | None


def parse_document(text: str) -> tuple[dict[str, Any], str]:
    """Parse markdown with optional YAML frontmatter.

    Returns ``(frontmatter, body)``. If the document has no frontmatter block,
    returns ``({}, text)`` (matching upstream OKFDocument.parse).

    Args:
        text: the raw file contents.

    Returns:
        A tuple of (frontmatter dict, body string). The body has a single
        trailing newline normalized for consistency.

    Raises:
        OKFParseError: if the frontmatter block is unterminated or contains
            invalid YAML, or if the YAML top-level is not a mapping.
    """
    if text is None:
        return {}, ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return {}, text

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            end_idx = i
            break
    if end_idx is None:
        raise OKFParseError("Unterminated YAML frontmatter block")

    fm_text = "\n".join(lines[1:end_idx])
    # P2-1: cap frontmatter bytes BEFORE parsing. Real OKF frontmatter is
    # <2 KiB; a payload over MAX_FRONTMATTER_BYTES is almost certainly a
    # resource-exhaustion attempt (huge literal). The node/materialized
    # caps below handle the small-source-but-huge-expansion alias bomb.
    fm_bytes = len(fm_text.encode("utf-8", errors="replace"))
    if fm_bytes > MAX_FRONTMATTER_BYTES:
        raise OKFParseError(
            f"Frontmatter is {fm_bytes} bytes, exceeds the "
            f"{MAX_FRONTMATTER_BYTES}-byte limit"
        )
    try:
        fm = safe_load_limited(fm_text) or {}
    except yaml.YAMLError as e:
        raise OKFParseError(f"Invalid YAML in frontmatter: {e}") from e
    if not isinstance(fm, dict):
        raise OKFParseError(
            f"Frontmatter must be a YAML mapping (got {type(fm).__name__})"
        )
    # P2-1: reject alias bombs whose serialized form would exceed MAX_NODES.
    # PyYAML keeps the alias graph compact in memory; this measures the cost
    # a serializer (render-time json.dumps) would actually pay.
    try:
        _assert_materialized_under_limit(fm)
    except _NodeBudgetExceeded as e:
        raise OKFParseError(f"Frontmatter rejected (DoS guard): {e}") from e

    body = "\n".join(lines[end_idx + 1 :])
    # Normalize leading blank line that follows the closing delimiter.
    if body.startswith("\n"):
        body = body[1:]
    return fm, body


def serialize_document(
    frontmatter: dict[str, Any], body: str, *, with_frontmatter: bool = True
) -> str:
    """Serialize a document back to markdown with YAML frontmatter.

    Round-trips key insertion order via ``yaml.safe_dump(sort_keys=False)``.
    Unknown keys are preserved (SPEC §9 mandate). The result always ends with
    a single trailing newline.
    """
    body = body if body.endswith("\n") else body + "\n"
    if not with_frontmatter or not frontmatter:
        return body
    fm_text = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip()
    return f"{FRONTMATTER_DELIM}\n{fm_text}\n{FRONTMATTER_DELIM}\n\n{body}"


def _strip_code_blocks(body: str) -> str:
    """Replace fenced code blocks and inline code with neutral placeholders
    so link scanning ignores code content.

    We replace fenced blocks with the same number of blank lines so line
    numbers in diagnostics remain accurate.
    """
    out_lines: list[str] = []
    in_fence = False
    fence_marker: str | None = None
    for line in body.splitlines():
        m = _FENCE_RE.match(line)
        if m and not in_fence:
            in_fence = True
            fence_marker = m.group("fence")[0]
            out_lines.append("")
            continue
        if in_fence and line.strip().startswith(fence_marker * 3):
            in_fence = False
            fence_marker = None
            out_lines.append("")
            continue
        out_lines.append("" if in_fence else line)
    return "\n".join(out_lines)


def _classify_and_resolve(
    target: str,
    source_dir: Path,
    bundle_root: Path,
    *,
    allow_host_lookup: bool = True,
) -> tuple[LinkForm, ConceptId | None, str | None]:
    """Classify a link target and resolve it to a concept id if internal.

    Args:
        target: raw link target.
        source_dir: directory containing the source document.
        bundle_root: absolute bundle root.

    Returns:
        (form, concept_id_or_None, anchor_or_None).
    """
    # Split off an optional #anchor before classification.
    if "#" in target:
        path_part, anchor = target.split("#", 1)
    else:
        path_part, anchor = target, None

    if not path_part or path_part.startswith("#"):
        # Python parses `a or b if c else d` as `(a or b) if c else d`, which
        # would drop the fragment for anchor-only links (`#intro` -> path_part
        # is "" -> falsy -> result None). Parenthesize to fix the precedence.
        return "anchor", None, anchor or (path_part[1:] if path_part else None)

    # External URIs.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", path_part) or path_part.startswith(
        ("mailto:", "tel:", "ftp:", "xmpp:")
    ):
        return "external", None, anchor

    # Absolute bundle-relative: /path/to/concept.md (SPEC §5.1, recommended)
    if path_part.startswith("/"):
        if not path_part.endswith(".md"):
            # Non-markdown absolute path (e.g. an asset) — not a concept link.
            return "external", None, anchor
        rel = path_part.lstrip("/")
        # Strip the .md suffix for the concept id.
        try:
            cid = concept_id_from_str(rel[:-3])
        except ConceptIdError:
            return "external", None, anchor
        return "absolute", cid, anchor

    # Relative: ./x.md, ../y/x.md, x.md (SPEC §5.2)
    if path_part.endswith(".md"):
        if not allow_host_lookup:
            import posixpath
            try:
                source_rel = source_dir.relative_to(bundle_root).as_posix()
            except ValueError:
                return "relative", None, anchor
            rel_text = posixpath.normpath(posixpath.join(source_rel, path_part))
            if rel_text == ".." or rel_text.startswith("../") or rel_text.startswith("/"):
                return "relative", None, anchor
            try:
                return "relative", concept_id_from_str(rel_text[:-3]), anchor
            except ConceptIdError:
                return "relative", None, anchor
        try:
            resolved = (source_dir / path_part).resolve()
            bundle_root_resolved = bundle_root.resolve()
            rel = resolved.relative_to(bundle_root_resolved)
        except (ValueError, OSError):
            # The path escapes the bundle root (out-of-bundle). This is
            # legal — bundles link to deep design docs, repo READMEs,
            # sibling bundle assets, etc. outside their own root.
            # Check disk existence: if the file exists in the workspace,
            # treat as external (not a graph edge, not broken). If not,
            # fall through to broken-link handling with a clearer signal.
            try:
                resolved_oor = (source_dir / path_part).resolve()
            except (ValueError, OSError):
                return "relative", None, anchor
            if resolved_oor.exists():
                # Valid out-of-bundle reference; not a graph edge.
                return "external_out_of_bundle", None, anchor
            return "relative", None, anchor
        try:
            from .paths import concept_id_from_path

            cid = concept_id_from_path(bundle_root, resolved)
        except (ConceptIdError, ValueError):
            return "relative", None, anchor
        return "relative", cid, anchor

    # A relative path that doesn't end in .md — treat as external (e.g. asset).
    return "external", None, anchor


def extract_links(
    body: str,
    *,
    source_dir: Path,
    bundle_root: Path,
    allow_host_lookup: bool = True,
) -> list[ExtractedLink]:
    """Extract all markdown links from a document body.

    Handles both absolute bundle-relative links (SPEC §5.1) and relative
    links (SPEC §5.2). Skips links inside fenced code blocks and inline code
    spans. External and anchor-only links are returned with their form
    classified but `concept_id=None`.

    SPEC §10 wikilinks (``[[target]]`` / ``[[target|Label]]``) are also
    extracted and emitted with ``form="wikilink"``; the target is resolved
    to a concept id when it parses as one. Wikilinks inside fenced/inline
    code are skipped just like markdown links.

    Args:
        body: the markdown body (no frontmatter).
        source_dir: absolute directory of the source document.
        bundle_root: absolute bundle root.

    Returns:
        List of ExtractedLink in document order. Markdown links and
        wikilinks are interleaved by their position in the body.
        Duplicates are kept (the caller can dedupe by
        ``(target_raw, line)`` if desired).
    """
    source_dir = Path(source_dir)
    bundle_root = Path(bundle_root)
    source = _strip_code_blocks(body)
    # Mask code syntax for scanning, but retain offsets and newlines so labels
    # can be recovered from the source without discovering links inside code.
    cleaned = _INLINE_CODE_RE.sub(
        lambda m: re.sub(r"[^\n]", "`", m.group(0)), source,
    )

    # Collect markdown-link and wikilink matches together, then sort by start
    # offset so the returned list is in deterministic document order. A
    # markdown link and a wikilink cannot start at the same offset (single
    # ``[`` vs double ``[[``), so the kind tiebreak is only a safety net.
    matches: list[tuple[int, str, "re.Match[str]"]] = []
    for m in _LINK_RE.finditer(cleaned):
        matches.append((m.start(), "md", m))
    for m in _WIKILINK_RE.finditer(cleaned):
        matches.append((m.start(), "wl", m))
    matches.sort(key=lambda t: (t[0], t[1]))

    out: list[ExtractedLink] = []
    for start, kind, m in matches:
        line = cleaned.count("\n", 0, start) + 1
        if kind == "md":
            label = _INLINE_CODE_RE.sub(r"\1", source[slice(*m.span("label"))])
            target = source[slice(*m.span("target"))]
            form, cid, anchor = _classify_and_resolve(
                target, source_dir, bundle_root,
                allow_host_lookup=allow_host_lookup,
            )
            out.append(
                ExtractedLink(
                    target_raw=target,
                    label=label,
                    form=form,
                    anchor=anchor,
                    line=line,
                    concept_id=cid,
                )
            )
        else:
            # Wikilink: target is a concept id string (no .md suffix).
            target = source[slice(*m.span(1))]
            label = (_INLINE_CODE_RE.sub(r"\1", source[slice(*m.span(2))])
                     if m.group(2) is not None else target)
            # P3-5: split `#anchor` like the markdown-link resolver
            # (_classify_and_resolve, L194-198) so `[[path#section]]` resolves
            # the concept id and surfaces the anchor instead of producing no
            # edge (concept_id_from_str rejects `#` as a segment char).
            anchor = None
            target_path = target
            if "#" in target:
                target_path, _, anchor = target.partition("#")
                target_path = target_path or target
            try:
                cid = concept_id_from_str(target_path)
            except ConceptIdError:
                cid = None
            out.append(
                ExtractedLink(
                    target_raw=target,
                    label=label,
                    form="wikilink",
                    anchor=anchor,
                    line=line,
                    concept_id=cid,
                )
            )
    return out


def extract_image_targets(body: str) -> list[tuple[str, int]]:
    """All markdown image targets in a body: ``[(target_raw, line), ...]``.

    Images are not links (no graph edges — the ``(?<!!)`` guard in
    ``_LINK_RE``), but their targets still name files the viewer must be
    able to serve; the validator uses this for asset-existence warnings.
    Fenced code blocks and inline code spans are skipped exactly like link
    extraction, and line numbers are 1-based body lines.
    """
    cleaned = _strip_code_blocks(body)
    cleaned = _INLINE_CODE_RE.sub("`", cleaned)
    out: list[tuple[str, int]] = []
    for m in _IMAGE_LINK_RE.finditer(cleaned):
        line = cleaned.count("\n", 0, m.start()) + 1
        out.append((m.group("target"), line))
    return out


# --- Markdown -> plain text for lexical search -------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*{1,3}|_{1,3}|~~)(.+?)\1", re.DOTALL)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_KEEP_LABEL_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TABLE_PIPE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_markdown_for_search(body: str) -> str:
    """Reduce a markdown body to plain text suitable for lexical indexing.

    - Strips code fences and inline code.
    - Drops images and HTML tags.
    - Replaces links with their label text (SPEC §5 links contribute the
      label, not the path, to the document's textual content).
    - Drops heading markers, emphasis, table pipes.
    - Collapses whitespace.

    The goal is a stable, low-noise text representation for BM25/TF-IDF.
    """
    if not body:
        return ""
    # Strip fenced code blocks entirely.
    pieces: list[str] = []
    in_fence = False
    fence_marker: str | None = None
    for line in body.splitlines():
        m = _FENCE_RE.match(line)
        if m and not in_fence:
            in_fence = True
            fence_marker = m.group("fence")[0]
            continue
        if in_fence and line.strip().startswith(fence_marker * 3):
            in_fence = False
            fence_marker = None
            continue
        if not in_fence:
            pieces.append(line)
    text = "\n".join(pieces)

    # Drop images, then collapse links to their labels.
    text = _IMAGE_RE.sub("", text)
    text = _LINK_KEEP_LABEL_RE.sub(r"\1", text)
    # Drop inline code backticks (keep contents).
    text = _INLINE_CODE_RE.sub(r"\1", text)
    # Drop HTML tags.
    text = _HTML_TAG_RE.sub(" ", text)
    # Drop heading markers (keep text).
    text = _HEADING_RE.sub(r"\2", text)
    # Drop emphasis markers.
    text = _EMPHASIS_RE.sub(r"\2", text)
    # Drop table border pipes (keep cell text).
    text = _TABLE_PIPE_RE.sub(lambda m: m.group(0).replace("|", " "), text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_headings(body: str) -> list[tuple[int, str, str]]:
    """Return headings as ``(level, slug, text)`` in document order.

    Slug is a lowercase, hyphen-joined, alphanumeric-only anchor suitable for
    matching the ``#anchor`` portion of a link. Empty headings are skipped.
    """
    out: list[tuple[int, str, str]] = []
    seen_slugs: dict[str, int] = {}
    # Strip fenced code blocks so we don't pick up # comments inside them.
    cleaned = _strip_code_blocks(body)
    for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*#*\s*$", cleaned, re.MULTILINE):
        level = len(m.group(1))
        text = m.group(2).strip()
        if not text:
            continue
        slug = _slugify(text)
        # Disambiguate duplicate slugs per CommonMark convention.
        if slug in seen_slugs:
            seen_slugs[slug] += 1
            slug = f"{slug}-{seen_slugs[slug]}"
        else:
            seen_slugs[slug] = 0
        out.append((level, slug, text))
    return out


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s\-]", "", text)
    text = re.sub(r"[\s\-]+", "-", text)
    return text.strip("-")
