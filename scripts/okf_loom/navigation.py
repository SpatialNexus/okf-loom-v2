"""Source-owned Markdown navigation, without filesystem or graph side effects."""
import html
import json
import posixpath
import re
from collections import Counter
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .documents import document_path
from .paths import concept_id_to_str

if TYPE_CHECKING:
    from .model import Concept
    from .retrieval import Passage


class _RenderedHeadingIds(HTMLParser):
    """Read identities and labels, not Markdown grammar, from renderer output."""

    # HTML void elements never carry content and never fire a matching end
    # tag in HTML5 syntax. Counting them as depth would leave the label
    # reader open across the heading's end (``<h2>Overview<br></h2>``) and
    # silently swallow every later heading's label.
    _VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = Counter()
        self.headings = {}
        self.labels = {}
        self._open = None      # fragment of the heading currently being read
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        fragment = attributes.get('id')
        if fragment:
            self.ids[fragment] += 1
        if self._depth:
            if tag not in self._VOID_TAGS:
                self._depth += 1
            return
        if tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'} and fragment:
            # Label capture opens for ANY id-carrying heading (the page body
            # render may not carry projection owner marks); owner mapping is
            # kept separately for the source prover.
            owner = attributes.get('data-okf-proj-owner')
            if owner:
                self.headings[owner] = fragment
            self._open = fragment
            self._depth = 1
            self._parts = []

    def handle_endtag(self, tag):
        if tag in self._VOID_TAGS or not self._depth:
            return
        self._depth -= 1
        if not self._depth:
            label = re.sub(r'\s+', ' ', ''.join(self._parts)).strip()
            self.labels[self._open] = label
            self._open = None
            self._parts = []

    def handle_data(self, data):
        if self._depth:
            self._parts.append(data)


def passage_heading_fragment(concept: 'Concept', passage: 'Passage') -> str | None:
    """Prove a derived passage's heading against this exact source and renderer.

    Internal source adapter, not a wire parser. Callers must also bind their
    result to its snapshot revision (RetrievalService.verified_section_fragment).
    Returns an actual decoded DOM id, never a URL or a line-citation fragment.
    Missing/ambiguous proof means ordinary document-root navigation. Plugins and
    client consumers must recheck identity inside the actual selected document;
    this source proof cannot promise a heading survives a render override.
    """
    if (passage.concept_id != concept.id
            or passage.path != concept.rel_path.as_posix()
            or passage.heading_line is None):
        return None
    prover = _concept_section_prover(concept)
    return prover[0](passage) if prover is not None else None


def _concept_section_prover(concept: 'Concept'):
    """Precompute the shared source-owned proof inputs for exactly one concept.

    One renderer pass per concept; the returned closure applies the identical
    per-passage checks as :func:`passage_heading_fragment`. ``None`` means the
    concept's body does not align with its source, so no passage of this
    concept can ever prove a heading.
    """
    from .retrieval import _body_start
    from .viewer.markdown import render_blocks_with_owners

    lines = concept.raw_text.replace('\r\n', '\n').split('\n')
    start = _body_start(lines)
    # parse_document drops one blank line immediately after frontmatter. Do
    # not infer a body by searching for repeated text elsewhere in the source.
    if start and start < len(lines) and lines[start] == '':
        start += 1
    body = concept.body.replace('\r\n', '\n')
    source_body = '\n'.join(lines[start:])
    if source_body not in (body, body + '\n'):
        return None
    # Offsets are code points in the LF/CRLF-normalized source. The renderer
    # returns original body offsets even when it normalizes lone CR internally.
    offsets = [0]
    for line in lines[start:]:
        offsets.append(offsets[-1] + len(line) + 1)
    rendered, owners = render_blocks_with_owners(body)
    headings = [owner for owner in owners if owner['kind'] == 'heading']
    identities = _RenderedHeadingIds()
    identities.feed(rendered)

    def prove(passage: 'Passage') -> str | None:
        if not (start < passage.heading_line <= passage.line_start
                <= passage.line_end <= len(lines)):
            return None
        excerpt = '\n'.join(lines[passage.line_start - 1:passage.line_end])
        if passage.char_start is not None or passage.char_end is not None:
            if (passage.line_start != passage.line_end
                    or passage.char_start is None or passage.char_end is None
                    or not 0 <= passage.char_start < passage.char_end <= len(excerpt)):
                return None
            excerpt = excerpt[passage.char_start:passage.char_end]
        if excerpt != passage.excerpt:
            return None
        heading_start = offsets[passage.heading_line - 1 - start]
        passage_end = (offsets[passage.line_end - 1 - start]
                       + (passage.char_end if passage.char_end is not None
                          else len(lines[passage.line_end - 1])))
        target = next((owner for owner in headings if owner['start'] == heading_start), None)
        if target is None:
            return None
        # Retrieval's section grammar is deliberately unchanged. If a passage
        # crosses a renderer-only heading (e.g. Setext), no single section is proven.
        if any(heading_start < owner['start'] < passage_end for owner in headings):
            return None
        fragment = identities.headings.get(target['nonce'])
        # Count all ids, including footnotes and recursively rendered quote headings.
        # The shipped duplicate suffixer can collide with an authored suffixed title.
        return fragment if fragment and identities.ids[fragment] == 1 else None

    def label(fragment: str) -> str:
        """Renderer-owned display label for one proven heading id."""
        text = identities.labels.get(fragment, "")
        if not text or len(text) > MAX_SECTION_NAVIGATION_HEADING_CHARS:
            return ""
        return text if _text_is_transport_safe(text) else ""

    # One renderer pass serves both identity proof and label extraction.
    return prove, label


def rendered_heading_labels(body_html: str) -> dict[str, str]:
    """Renderer-owned heading id -> display label map for one rendered body.

    Labels are entity-decoded, anchor-stripped, whitespace-normalized text —
    the same identity the browser's ``textContent`` yields for the heading
    element, so consumers can re-admit the exact heading inside the displayed
    document. Bounded: over-long labels are omitted rather than truncated.
    """
    parser = _RenderedHeadingIds()
    parser.feed(body_html)
    return {fragment: label for fragment, label in parser.labels.items()
            if label and parser.ids[fragment] == 1
            and len(label) <= MAX_SECTION_NAVIGATION_HEADING_CHARS
            and _text_is_transport_safe(label)}


# Optional bounded presentation transport for portable builds. This is
# deliberately NOT part of the strict schema-v1 passage/corpus records: it
# travels as separate sibling metadata, and every consumer must treat absent,
# stale, or malformed metadata as "no section destinations" (ordinary
# document-root reading).
SECTION_NAVIGATION_SCHEMA = 2
MAX_SECTION_NAVIGATION_ENTRIES = 20000
MAX_SECTION_NAVIGATION_FRAGMENT_CHARS = 512
MAX_SECTION_NAVIGATION_HEADING_CHARS = 256
MAX_SECTION_NAVIGATION_REVISION_CHARS = 256
MAX_SECTION_NAVIGATION_BYTES = 2 * 1024 * 1024
INTENT_PARAM_FRAGMENT = "okf-section"
INTENT_PARAM_REVISION = "okf-rev"
INTENT_PARAM_CONCEPT = "okf-c"
INTENT_PARAM_HASH = "okf-h"

_FNV_OFFSET = 0xcbf29ce484222325
_FNV_PRIME = 0x100000001b3
_FNV_MASK = 0xffffffffffffffff


def section_intent_hash(label: str) -> str:
    """Deterministic 64-bit FNV-1a of a heading label, as 16 lowercase hex.

    The navigation intent carries this short binding instead of the full
    label, so the destination can prove the DOM heading still has the exact
    renderer-owned text the search result was proven against (plugin- or
    script-modified headings fail closed to document-root reading).
    """
    digest = _FNV_OFFSET
    for byte in label.encode("utf-8"):
        digest ^= byte
        digest = (digest * _FNV_PRIME) & _FNV_MASK
    return f"{digest:016x}"


def _fragment_is_transport_safe(fragment: str) -> bool:
    """DOM ids are free-form; the transport still refuses hostile characters."""
    if not fragment or len(fragment) > MAX_SECTION_NAVIGATION_FRAGMENT_CHARS:
        return False
    return _text_is_transport_safe(fragment)


def _text_is_transport_safe(text: str) -> bool:
    return not any(
        ord(char) < 0x20 or 0x7f <= ord(char) <= 0x9f
        or 0x202a <= ord(char) <= 0x202e or 0x2066 <= ord(char) <= 0x2069
        for char in text
    )


def build_section_navigation(bundle: 'Bundle') -> dict | None:
    """Build bounded optional section-destination metadata for this snapshot.

    Maps exact schema-v1 ``passage_id`` strings to the renderer-owned heading
    id and display label proven against the same source and renderer rules as
    :func:`passage_heading_fragment`. Each entry also carries the concept
    identity and that concept's per-document normalized-source revision
    (:func:`annotations.concept_content_rev` of the concept's LF-normalized
    raw source — the canonical source the renderer consumed, never served
    disk bytes and never the retrieval corpus revision, which stays owned by
    the payload's top-level ``bundle_revision`` via
    ``RetrievalService.revision``). The per-concept scope is deliberate: an
    unrelated concept edit cannot move this entry's revision, and two
    concepts with identical content share a revision, so destination
    admission must match concept identity too. Consumers must independently
    establish textual query evidence per result, match the row's concept
    identity, and re-admit the heading inside the actually displayed document
    against the destination's own identity and revision proof; the map alone
    proves none of those. Returns ``None`` when nothing is provable, a cap is
    exceeded, or the payload exceeds the byte bound — never a partial map —
    so absent metadata stays the honest fallback.
    """
    from .annotations import concept_content_rev
    from .retrieval import RetrievalService

    with bundle._derived_lock:
        service = RetrievalService(bundle)
        revision = service.revision
        by_concept: dict = {}
        for passage in service.passages:
            if passage.heading_line is None:
                continue
            by_concept.setdefault(passage.concept_id, []).append(passage)
        fragments: dict[str, dict] = {}
        for cid in sorted(by_concept):
            concept = bundle.concepts.get(cid)
            if concept is None:
                continue
            prover = _concept_section_prover(concept)
            # Misaligned in-memory source/body means no passage of this
            # concept can prove a heading: skip the concept, never raise.
            if prover is None:
                continue
            prove, label_for = prover
            concept_revision = concept_content_rev(
                concept.raw_text.replace("\r\n", "\n"))
            if len(concept_revision) > MAX_SECTION_NAVIGATION_REVISION_CHARS:
                return None
            for passage in by_concept[cid]:
                fragment = prove(passage)
                label = label_for(fragment) if fragment else ""
                if (not fragment or not _fragment_is_transport_safe(fragment)
                        or not label):
                    continue
                fragments[passage.passage_id] = {
                    "concept": concept_id_to_str(cid),
                    "fragment": fragment,
                    "heading": label,
                    "source_revision": concept_revision,
                }
                if len(fragments) > MAX_SECTION_NAVIGATION_ENTRIES:
                    return None
        if not fragments:
            return None
        payload = {
            "schema": SECTION_NAVIGATION_SCHEMA,
            "bundle_revision": revision,
            "fragments": fragments,
        }
        if (len(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":")))
                > MAX_SECTION_NAVIGATION_BYTES):
            return None
        return payload


def heading_fragments(body: str) -> set[str]:
    """Use the shipped renderer's actual heading grammar/slug identities."""
    from .viewer.markdown import markdown_to_html
    return {html.unescape(value) for value in re.findall(r'<h[1-6] id="([^"]+)"', markdown_to_html(body))}


def resolve_document_link(source: str, href: str) -> tuple[str, str] | None:
    """Resolve Markdown links lexically; refuse escapes rather than clamp them."""
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.query or '\\' in href:
        return None
    path = parsed.path
    if not path:
        return (source, parsed.fragment) if parsed.fragment else None
    if not path.endswith('.md'):
        return None
    target = posixpath.normpath(path.lstrip('/') if path.startswith('/') else
                               posixpath.join(posixpath.dirname(source), path))
    try:
        return document_path(target), parsed.fragment
    except ValueError:
        return None


def normalize_document_links(rendered: str, source: str, mode: str, *,
                             page_url: str | None = None) -> str:
    """Normalize renderer-owned hrefs from the selected source, not the graph URL.

    Graph payloads use serve-form canonical Markdown URLs. Portable graph clients
    translate those canonical identities to their own navigation destinations.
    A body rendered on its own document page may supply that page's mode-specific
    URL so both canonical self-links and already-rewritten self-links retain
    native fragments. Graph payloads must omit it: their host is not the source.
    """
    def replace(match):
        href = html.unescape(match.group(2))
        path, _, fragment = href.partition('#')
        if page_url is not None and path == page_url and fragment:
            return match.group(1) + html.escape('#' + fragment, quote=True) + '"'
        target = resolve_document_link(source, href)
        if target is None:
            return match.group(0)
        document, fragment = target
        if page_url is not None and document == source and fragment:
            dest = ''
        elif mode == 'static':
            dest = posixpath.relpath(document[:-3] + '.html', posixpath.dirname(source) or '.')
        else:
            dest = '/' + document
        if fragment:
            dest += '#' + fragment
        return match.group(1) + html.escape(dest, quote=True) + '"'
    return re.sub(r'(<a\b[^>]*\bhref=")([^"]*)"', replace, rendered)
