"""Single-file + static-site rendering for OKF bundles.

Public API:
    render_single_file(bundle, out_path, *, name=None) -> dict
    build_site(bundle, out_dir, *, target, name=None) -> dict

Both functions are pure-Python (stdlib + pyyaml). The single-file viewer
embeds the bundle as JSON and uses Cytoscape.js from CDN (markdown is
rendered server-side; no client-side parser). The static/spa site is a
directory of server-rendered HTML pages sharing templates with the live
wiki server.

Hard constraints honoured: zero hard deps beyond pyyaml, no Jinja2/markdown/
watchdog, atomic writes for build output.
"""
from __future__ import annotations
from .navigation import (build_section_navigation, normalize_document_links,
                         section_intent_hash, INTENT_PARAM_FRAGMENT,
                         INTENT_PARAM_HASH, INTENT_PARAM_REVISION,
                         INTENT_PARAM_CONCEPT,
                         MAX_SECTION_NAVIGATION_BYTES,
                         rendered_heading_labels)
from .review_links import inject_review_metadata
from .portable_feedback_export import (
    ASSET_NAME as PORTABLE_FEEDBACK_ASSET,
    PortableFeedbackTargets,
    collect_targets as collect_portable_feedback_targets,
    inject_before_head_close as inject_portable_feedback_bootstrap,
    page_bootstrap_script as page_portable_feedback_script,
    single_file_bootstrap_script as single_file_portable_feedback_script,
)

import json
import hashlib
import base64
import html as _html
import math
import os
import posixpath
import re
import tempfile
from datetime import timezone
from html.parser import HTMLParser
from html.entities import html5 as _html5_entities
from urllib.parse import quote as _url_quote, urlencode, unquote as _url_unquote
from pathlib import Path
from typing import Any

from .aliases import alias_labels
from .metadata import effective_instant_key, parse_instant
from .viewer.metadata import (FIELDS as METADATA_FIELDS, render_metadata, join_source_footnotes,
                              render_search_advisory)
from .model import Bundle, Concept
from .paths import ConceptId, concept_id_from_str, concept_id_to_str
from .retrieval import bundle_revision
from .annotations import extract_blocks, concept_content_rev
from .studio import (
    CommentSummaryUnavailable,
    CommentSummaryV1,
    canonical_comment_summary_revision,
    read_comment_summary,
    read_event_snapshot,
)
from .viewer.assets import (
    auto_palette,
    list_builtin_static,
    load_config,
    load_palette_override,
    load_static,
    load_template,
    resolve_palette,
    stable_hue,
    hsl_color,
    type_color_for,
    type_icon_svg,
    type_icon_key,
    type_icon_paths,
    _sanitize_css_color,
)
from .viewer.markdown import (
    markdown_to_html,
    rewrite_internal_links,
    url_for_concept,
)
from .viewer.shell import (
    ApplicationShell,
    render_application_shell,
    render_single_file_shell,
)


_PROJECTION_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "tr", "hr", "div"})
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*(?:\r?\n)?$")
_THEMATIC_BREAK = re.compile(r"^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$")
# Footnote-definition line shape: the extractor classifies these as ``paragraph``
# but the renderer emits them as ``<li>`` inside the footnotes section.
_FOOTNOTE_DEF_SHAPE = re.compile(r"^\[\^[^\]\s]+]:\s+.+", re.DOTALL)

# Wide authored blocks should own their overflow instead of widening the
# document. These wrappers are server-rendered so the same local scrollport
# exists in live, static, SPA, and single-file output before enhancements run.
_CONTENT_PRE_RE = re.compile(r"<pre(\s[^>]*)?>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
_CONTENT_DIAGRAM_RE = re.compile(
    r"<div(\s+[^>]*class=[\"'][^\"']*\b(?:mermaid|math|math-inline)\b[^\"']*[\"'][^>]*)>(.*?)</div>",
    re.DOTALL | re.IGNORECASE,
)
_CONTENT_FIGURE_RE = re.compile(r"<figure(\s[^>]*)?>(.*?)</figure>", re.DOTALL | re.IGNORECASE)


def _decorate_content_scrollports(html: str) -> str:
    """Add structural metadata around blocks that may become scroll regions."""

    def _metadata(kind: str, label: str) -> str:
        return (
            f'class="okf-content-scrollport okf-content-scrollport--{kind}" '
            f'data-okf-scroll-kind="{_esc_attr_qs(kind)}" '
            f'data-okf-scroll-label="{_esc_attr_qs(label)}"'
        )

    def _wrap_pre(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        inner = match.group(2)
        language = re.search(r'\blanguage-([\w+-]+)', inner, re.IGNORECASE)
        label = f"{language.group(1)} code block" if language else "Code block"
        return f'<div {_metadata("code", label)}><pre{attrs}>{inner}</pre></div>'

    def _wrap_diagram(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        label = "Math expression" if re.search(r'\bmath(?:-inline)?\b', attrs) else "Diagram"
        return f'<div {_metadata("diagram", label)}><div{attrs}>{match.group(2)}</div></div>'

    def _decorate_figure(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        if re.search(r"\bclass\s*=", attrs, re.IGNORECASE):
            attrs = re.sub(
                r"\bclass\s*=\s*([\"'])(.*?)\1",
                lambda m: f'class={m.group(1)}{m.group(2)} okf-content-scrollport{m.group(1)}',
                attrs,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            attrs = f'{attrs} class="okf-content-scrollport"'
        caption = re.search(r"<figcaption(?:\s[^>]*)?>(.*?)</figcaption>", match.group(2), re.DOTALL | re.IGNORECASE)
        caption_text = re.sub(r"<[^>]+>", " ", caption.group(1)) if caption else ""
        caption_text = re.sub(r"\s+", " ", caption_text).strip()
        label = f"Figure: {caption_text}" if caption_text else "Figure or media"
        attrs += f' data-okf-scroll-kind="media" data-okf-scroll-label="{_esc_attr_qs(label)}"'
        return f"<figure{attrs}>{match.group(2)}</figure>"

    html = _CONTENT_PRE_RE.sub(_wrap_pre, html)
    html = _CONTENT_DIAGRAM_RE.sub(_wrap_diagram, html)
    return _CONTENT_FIGURE_RE.sub(_decorate_figure, html)

# ---------------------------------------------------------------------------
# HTML5 tree-construction repair constructs that the stdlib ``HTMLParser``
# does NOT model. When any of these appear in rendered HTML, a browser's
# actual DOM tree differs from the ``HTMLParser`` nesting — so textContent
# extraction per owner nonce would be unreliable. The projection finalizer
# detects them and fails closed (``complete=False``) rather than emit a
# silently-wrong source map. (ReviewAll r7: "projection validation is not
# browser-tree complete".)
# ---------------------------------------------------------------------------
# Formatting elements subject to the adoption agency algorithm.
_HTML5_FORMATTING_TAGS = frozenset({
    "a", "b", "big", "code", "em", "font", "i", "nobr", "s",
    "small", "strike", "strong", "tt", "u",
})
# Foreign-content roots with fundamentally different tree-construction rules.
_HTML5_FOREIGN_ROOTS = frozenset({"svg", "math"})
# Block-level elements that trigger formatting reconstruction when a formatting
# element is still open.
_HTML5_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "details", "dialog",
    "div", "dl", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "main",
    "menu", "nav", "ol", "p", "pre", "section", "table", "ul",
})
# Implied end-tag groups: start tag -> (open tags it auto-closes, scope
# barriers that terminate the backward stack scan).
_HTML5_IMPLIED_CLOSER: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol", "menu"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "option": (frozenset({"option"}), frozenset({"select"})),
    "optgroup": (frozenset({"option", "optgroup"}), frozenset({"select"})),
    "tr": (frozenset({"tr"}), frozenset({"table", "tbody", "thead", "tfoot"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "thead": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tbody": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tfoot": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
}
# Table-context elements where non-table-content is foster-parented. A row is
# one too: anything but a cell directly inside ``tr`` leaves the table.
_HTML5_TABLE_FOSTER_CTX = frozenset({"table", "thead", "tbody", "tfoot", "colgroup", "tr"})
# Table cell / caption elements that establish a non-foster position.
_HTML5_TABLE_CELL_TAGS = frozenset({"td", "th", "caption"})
# Valid direct children per table-context element (others are foster-parented).
_HTML5_TABLE_VALID_DIRECT: dict[str, frozenset[str]] = {
    "table": frozenset({"caption", "colgroup", "col", "thead", "tbody", "tfoot", "tr", "form", "script", "style", "template"}),
    "thead": frozenset({"tr", "script", "style", "template"}),
    "tbody": frozenset({"tr", "script", "style", "template"}),
    "tfoot": frozenset({"tr", "script", "style", "template"}),
    "colgroup": frozenset({"col", "template"}),
    "tr": frozenset({"td", "th", "script", "style", "template"}),
}
# HTML-namespace elements that bound HTML5 "button scope" (foreign roots
# already fail closed).
_HTML5_BUTTON_SCOPE_BARRIERS = frozenset({
    "applet", "button", "caption", "html", "marquee", "object", "table",
    "td", "th", "template",
})
# HTML's ASCII whitespace. Python's str.strip() also drops U+00A0 and other
# Unicode spaces, which HTML5 treats as text (and foster-parents in a table).
_HTML_ASCII_WHITESPACE = "\t\n\x0c\r "
# The end tag that closes noscript raw text (scripting enabled).
_NOSCRIPT_RAW_TEXT_END = re.compile(r"</noscript[\t\n\x0c\r />]", re.IGNORECASE)
_HTML5_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Structural allowlist: the only elements that may be a keyed owner, enclose
# one, or sit inside one. Admission models their whole in-body tree
# construction: each either only inserts an element, or also closes an open
# p (div-like containers, lists, headings, pre, hr, table), and the p,
# list-item, heading, anchor, pre and table rules in the finalizer cover the
# rest. Any other element there (button, select/option/optgroup, form, nobr,
# the ruby family, unknown or custom elements, ...) makes the map incomplete.
_PROJECTION_MODELED_TAGS = frozenset({
    # Renderer blocks, lists and tables: owners and their wrappers.
    "blockquote", "div", "hr", "li", "ol", "p", "pre", "section", "ul",
    "table", "tbody", "td", "th", "thead", "tr", *_HTML5_HEADINGS,
    # Containers whose start tag behaves exactly like div and section.
    "address", "article", "aside", "figcaption", "figure", "footer",
    "header", "main", "nav",
    # Definition lists (dd/dt implied closing is modelled).
    "dd", "dl", "dt",
    # Renderer phrasing and plain phrasing without insertion-mode effects.
    "a", "abbr", "b", "bdi", "bdo", "br", "cite", "code", "data", "del",
    "dfn", "em", "i", "img", "input", "ins", "kbd", "mark", "q", "s", "samp",
    "small", "span", "strong", "sub", "sup", "time", "u", "var", "wbr",
})
# Table parts and the only open element they may start in, anywhere in the
# document. In a cell they close the cell (and anything open inside it), so
# the rest of that cell, owners included, is foster-parented out of the
# table; elsewhere in body the parser drops them, so an owner row whose table
# is missing loses its element. col may also start directly in table, where
# HTML5 implies its colgroup. caption, colgroup, col and tfoot are not
# allowlisted, so they still cannot enclose or sit inside an owner.
_HTML5_TABLE_PART_PARENTS: dict[str, frozenset[str]] = {
    "caption": frozenset({"table"}),
    "colgroup": frozenset({"table"}),
    "col": frozenset({"table", "colgroup"}),
    "tfoot": frozenset({"table"}),
    "thead": frozenset({"table"}),
    "tbody": frozenset({"table"}),
    "tr": frozenset({"table", "thead", "tbody", "tfoot"}),
    "td": frozenset({"tr"}),
    "th": frozenset({"tr"}),
}
# HTML5 comment syntax: after "<!--", text that does not start with ">" or
# "->", nor contain "<!--", "-->" or "--!>", nor end with "<!-", then "-->".
# Python's HTMLParser ends other comments elsewhere than a browser does
# ("<!-->" is a whole comment in HTML5), so markup one of them hides the other
# parses, even before the owners.
_HTML5_COMMENT_FORBIDDEN = ("<!--", "-->", "--!>")


def _html5_comment_well_formed(data: str) -> bool:
    return (not data.startswith((">", "->")) and not data.endswith("<!-")
            and not any(s in data for s in _HTML5_COMMENT_FORBIDDEN))


def _html5_numeric_reference(name: str) -> str:
    """The code point HTML5 tokenization emits for ``&#name;``.

    ``html.unescape`` drops the C0 controls other than NUL, tab, LF, FF and
    CR, and DEL and the noncharacters; HTML5 emits every one of them.
    """
    num = int(name[1:], 16) if name[:1] in "xX" else int(name)
    if num == 0 or num > 0x10FFFF or 0xD800 <= num <= 0xDFFF:
        return "\ufffd"
    if 0x80 <= num <= 0x9F:
        try:
            return bytes([num]).decode("cp1252")
        except UnicodeDecodeError:  # 0x81, 0x8D, 0x8F, 0x90, 0x9D stay.
            return chr(num)
    return chr(num)


def _html5_owned_text_ok(raw: str, value: str) -> bool:
    """Whether HTML5 yields *value*, Python's text for token *raw*, in an owner.

    HTML5 input preprocessing turns a literal CR (and CRLF) into LF, which
    the renderer's CRLF handling never leaves in its output, and a numeric
    reference must decode as HTML5 decodes it. Literal NUL, which HTML5 drops
    or replaces in every context, is refused for the whole document.
    """
    if raw.startswith("&#"):
        return value == _html5_numeric_reference(raw[2:-1])
    return "\r" not in raw


class _OwnerTextScanner(HTMLParser):
    """Extract the textContent of each ``data-okf-proj-owner``-marked element.

    The renderer tags each projectable block's containing element with an
    opaque owner nonce during rendering. This scanner reads back the *actual*
    textContent per nonce after the plugin has run, so the finalizer can verify
    exact visible text and detect removals/duplications/forgery.
    """

    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str | None] = []
        self.text: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): v for k, v in attrs}
        nonce = attr_map.get("data-okf-proj-owner")
        if tag not in self._VOID:
            self.stack.append(nonce)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing: text accumulates to current ancestors only.
        pass

    def handle_endtag(self, tag: str) -> None:
        if self.stack:
            self.stack.pop()

    def _append(self, value: str) -> None:
        for nonce in {n for n in self.stack if n}:
            self.text[nonce] = self.text.get(nonce, "") + value

    def handle_data(self, data: str) -> None: self._append(data)
    def handle_entityref(self, name: str) -> None: self._append(_html.unescape(f"&{name};"))
    def handle_charref(self, name: str) -> None: self._append(_html.unescape(f"&#{name};"))


def _body_offset(source: str, body: str) -> int | None:
    """Return the exact body start in canonical source, or fail closed."""
    if not body:
        return len(source)
    positions = [m.start() for m in re.finditer(re.escape(body), source)]
    return positions[-1] if positions else None


class _ProjectionTextPositions(HTMLParser):
    """Exact lexical positions, not an HTML5 tree or a source of authority.

    Used only alongside the finalizer's structural/repair checks. Every code
    point must belong to a recognized, closed parser event.
    Parser recovery, raw-text contexts and uncertain coverage grant no text
    exemptions. Subtrees are compared to private pre-plugin renderer output;
    equal text alone, or a code tag, is never provenance.

    Every element must be closed. Only the returned-output check may pass
    *allow_trailing_open*: elements still open at end of document are then
    tolerated when none of them is or contains a marked element, because the
    browser closes them without moving earlier marked nodes. A marked element
    inside a ``noscript`` element, or a ``noscript`` inside a marked element,
    is always invalid: a scripting browser parses ``noscript`` content as
    unrendered raw text, so the marked text is not in the visible DOM.

    Where HTML5 tokenizes differently from Python's parser the scan is
    invalid: any comment that is not well-formed HTML5 comment syntax, any
    literal NUL, and in marked text a literal CR or a numeric reference whose
    ``html.unescape`` value differs from HTML5's.
    """

    def __init__(self, html: str, marker: str = "data-okf-proj-owner", *,
                 allow_trailing_open: bool = False) -> None:
        super().__init__(convert_charrefs=False)
        self.html = html
        self.marker = marker
        self.valid = True
        self.cursor = 0
        self.lines = [0] + [m.end() for m in re.finditer("\n", html)]
        self.stack: list[tuple[str, dict[str, Any] | None]] = []
        # Parallel to ``stack``: the open element is or contains a marked one.
        self.open_wraps: list[bool] = []
        self.elements: dict[str, list[dict[str, Any]]] = {}
        if "\x00" in html:  # HTML5 drops or replaces NUL; Python keeps it.
            self.valid = False
        try:
            self.feed(html)
            self.close()
        except Exception:
            self.valid = False
        closed = not self.stack or (allow_trailing_open and not any(self.open_wraps))
        self.valid = self.valid and self.cursor == len(html) and closed

    def _position(self) -> int:
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def _consume(self, raw: str) -> int:
        pos = self._position()
        if pos != self.cursor or not self.html.startswith(raw, pos):
            self.valid = False
        self.cursor = pos + len(raw)
        return pos

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ""
        pos = self._consume(raw)
        if tag in {"script", "style", "textarea", "title", "plaintext", "xmp",
                   "iframe", "noembed", "noframes", "template"}:
            self.valid = False
        values = [v for k, v in attrs if k == self.marker]
        element = None
        if values:
            if len(values) != 1 or values[0] is None:
                self.valid = False
            else:
                element = {"start": pos, "end": self.cursor, "text": "", "ranges": []}
                self.elements.setdefault(values[0], []).append(element)
                self.open_wraps = [True] * len(self.open_wraps)
        # A marked element and a noscript element nest (either way round):
        # a scripting browser keeps that text out of the visible DOM.
        if ((tag == "noscript" or any(t == "noscript" for t, _ in self.stack))
                and (element is not None or any(e is not None for _, e in self.stack))):
            self.valid = False
        if tag not in _OwnerTextScanner._VOID:
            self.stack.append((tag, element))
            self.open_wraps.append(element is not None)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _OwnerTextScanner._VOID:
            # HTML does not self-close non-void elements.
            self.valid = False
            self.stack.pop()
            self.open_wraps.pop()

    def handle_endtag(self, tag: str) -> None:
        match = re.match(r"</[a-zA-Z][a-zA-Z0-9:-]*\s*>", self.html[self._position():])
        if match is None:
            self.valid = False
            return
        self._consume(match.group())
        if not self.stack or self.stack[-1][0] != tag:
            self.valid = False
            return
        _, element = self.stack.pop()
        self.open_wraps.pop()
        if element is not None:
            element["end"] = self.cursor

    def _text(self, raw: str, value: str, *, literal: bool = False) -> None:
        pos = self._consume(raw)
        if (any(element is not None for _, element in self.stack)
                and not _html5_owned_text_ok(raw, value)):
            self.valid = False
        for _, element in self.stack:
            if element is not None:
                at = len(element["text"])
                element["text"] += value
                if literal:
                    element["ranges"].append((pos, self.cursor, at))

    def handle_data(self, data: str) -> None:
        if "<" in data:  # HTMLParser.close can recover malformed tags as data.
            self.valid = False
        self._text(data, data, literal=True)

    def handle_entityref(self, name: str) -> None:
        raw = f"&{name};"
        self._text(raw, _html.unescape(raw))

    def handle_charref(self, name: str) -> None:
        raw = f"&#{name};"
        self._text(raw, _html.unescape(raw))

    def handle_comment(self, data: str) -> None:
        # Exactly "<!--", well-formed HTML5 comment text, "-->".
        self._consume(f"<!--{data}-->")
        if not _html5_comment_well_formed(data):
            self.valid = False

    def handle_decl(self, decl: str) -> None:
        self.valid = False

    def unknown_decl(self, data: str) -> None:
        self.valid = False

    def handle_pi(self, data: str) -> None:
        self.valid = False


def _authored_key_ranges(
    trusted_html: str | None, rendered_html: str, owners: list[dict[str, Any]],
    mapped_owners: dict[str, int], source: str, offset: int,
) -> list[tuple[int, int]]:
    """Grant only literal, identity-source-backed text in unchanged owners.

    This proves observable exact subtree equality, not historical DOM identity:
    a string-returning plugin can replace a subtree with byte-identical HTML.
    A missing private comparison, source gap or uncertain parse grants nothing.
    """
    if trusted_html is None:
        return []
    trusted = _ProjectionTextPositions(trusted_html)
    current = _ProjectionTextPositions(rendered_html)
    if not trusted.valid or not current.valid:
        return []
    ranges = []
    for owner in owners:
        nonce = owner["nonce"]
        before = trusted.elements.get(nonce, [])
        after = current.elements.get(nonce, [])
        if nonce not in mapped_owners or len(before) != 1 or len(after) != 1:
            continue
        a, b = before[0], after[0]
        if (a["text"] != owner["expected_visible"] or b["text"] != a["text"]
                or trusted_html[a["start"]:a["end"]] != rendered_html[b["start"]:b["end"]]):
            continue
        for start, end, dom_start in b["ranges"]:
            for match in re.finditer("data-okf-projection-key", rendered_html[start:end], re.I):
                lo, hi = dom_start + match.start(), dom_start + match.end()
                # An identity segment covers the ENTIRE literal; never bridge
                # an unsupported source gap or borrow text from another owner.
                for s in owner["segments"]:
                    if (s["dom_start"] <= lo < hi <= s["dom_end"]
                            and s["dom_end"] - s["dom_start"] == s["source_end"] - s["source_start"]):
                        src = offset + s["source_start"] + lo - s["dom_start"]
                        if source[src:src + hi - lo] == match.group():
                            ranges.append((start + match.start(), start + match.end()))
                            break
    return sorted(set(ranges))


# ---------------------------------------------------------------------------
# Owner/key capability leak detection + scrubbing.
#
# A browser decodes HTML entities in text content, so capability material
# (``data-okf-proj-owner``/``data-okf-projection-key`` attribute names or
# registered owner nonce values) may be entity-encoded to evade literal
# substring leak checks. Detection decodes before matching; scrubbing removes
# the literal *and* entity/percent-encoded forms from the returned HTML so
# contaminated material never survives finalization.
#
# ReviewAll r10: bounded iterative canonicalization policy. Detection uses a
# BOUNDED ITERATIVE canonicalizer that repeatedly decodes HTML entities
# (named, decimal NCR, hex NCR — including ``&hyphen;``) and percent encodings
# until a fixed point or ``_CANONICALIZE_MAX_ITERATIONS`` is reached. This
# catches double-encoded entities (``&amp;#45;`` → ``&#45;`` → ``-``) and
# percent-encoded capability material (``%64%61%74%61`` → ``data``) that a
# single-pass ``_html.unescape`` misses. The scrub regex patterns are extended
# with named-entity and percent-encoding alternatives so the same material is
# removed from returned ``final_html``, not merely detected. Malformed /
# unterminated tag-like capability text is scrubbed fail-safe instead of
# copied verbatim. Post-scrub canonicalization re-verifies capability-free
# output; any residual contamination forces ``complete=False``.
# (Supersedes r9 #10/#11.)
# ---------------------------------------------------------------------------

# Reverse named-entity map: character → list of entity names (without &/;).
# Used to extend capability scrub patterns with named-entity alternatives so
# that named-entity-encoded capability material (e.g. ``&hyphen;`` for ``-``,
# or any future entity) is scrubbed alongside literal/NCR forms.
_NAMED_ENTITY_BY_CHAR: dict[str, list[str]] = {}
for _ent_name, _ent_char in _html5_entities.items():
    _clean = _ent_name.rstrip(";")
    if _clean:
        _NAMED_ENTITY_BY_CHAR.setdefault(_ent_char, []).append(_clean)

# Bounded iteration limit for iterative canonicalization. Four iterations
# catch up to quadruple encoding; a browser never decodes more than once in
# text content, but the canonicalizer is deliberately more conservative for
# capability detection. The bound also prevents pathological input from
# causing unbounded re-decoding loops.
_CANONICALIZE_MAX_ITERATIONS = 4


def _decode_layer(text: str) -> str:
    """One HTML-entity (named / decimal-NCR / hex-NCR) + percent-encoding
    decode pass. Shared by detection (``_canonicalize_iteratively``) and the
    bounded iterative scrub so both apply byte-identical decoding semantics."""
    text = _html.unescape(text)
    try:
        text = _url_unquote(text, errors="replace")
    except Exception:
        pass
    return text


def _canonicalize_iteratively(text: str) -> tuple[str, bool, bool]:
    """Bounded iterative canonicalization: repeatedly decode HTML entities
    (named, decimal NCR, hex NCR) and percent encodings until a fixed point
    or ``_CANONICALIZE_MAX_ITERATIONS`` iterations.

    Returns ``(canonical_text, converged, exhausted)``. ``converged`` is True
    when a fixed point was reached within the budget; ``exhausted`` is its
    complement — the budget was consumed without the decoded form stabilising.
    Exhaustion means the input carries encoding deeper than the canonicalizer
    can peel, so the caller MUST treat the output as un-trustworthy and force
    a capability-free safe fallback (``complete=False``): it cannot prove the
    decoded form is free of nested-encoded capability material.

    A browser decodes HTML entities in text content in a single pass and does
    NOT percent-decode text, but this canonicalizer is deliberately more
    conservative — it peels successive encoding layers so double-encoded
    entities (``&amp;#45;`` → ``&#45;`` → ``-``) and percent-encoded capability
    material (``%64%61%74%61`` → ``data``) are detected for fail-closed
    projection, even though they would not decode in a browser. The bound
    prevents unbounded re-decoding on pathological input.

    (ReviewAll r11: return convergence/exhaustion state alongside the
    canonical text so exhaustion forces a capability-free safe fallback /
    incomplete projection rather than a silently under-decoded verdict.)
    """
    converged = True
    for _ in range(_CANONICALIZE_MAX_ITERATIONS):
        prev = text
        text = _decode_layer(text)
        if text == prev:
            converged = True
            break
    else:
        # Loop completed without a fixed point → budget exhausted.
        converged = False
    return text, converged, (not converged)


def _capability_char_alt(ch: str) -> str:
    """Regex alternative matching one character literally, as an HTML numeric
    character reference (decimal or hex, any case, optional leading zeros), as
    a named entity, or as a percent encoding — so entity/percent-encoded
    capability material is matched alongside the literal form for both
    detection and scrubbing."""
    n = ord(ch)
    alts = [re.escape(ch), rf"&#0*{n};", rf"&#[Xx]0*{n:X};"]
    # Named entities that decode to this exact character (e.g. ``&hyphen;``
    # for U+2010). For current capability characters (ASCII letters, digits,
    # hyphen-minus U+002D) there are none, but the policy is established for
    # thoroughness and future-proofing.
    for name in _NAMED_ENTITY_BY_CHAR.get(ch, ()):
        alts.append(re.escape(f"&{name};"))
    # Percent encoding (uppercase and lowercase hex). Only meaningful for
    # ASCII characters (single-byte percent encoding).
    if n < 0x80:
        alts.append(re.escape(f"%{n:02X}"))
        alts.append(re.escape(f"%{n:02x}"))
    return "(?:" + "|".join(alts) + ")"


def _capability_literal_pattern(literal: str) -> str:
    """Case-insensitive pattern string matching *literal* where each character
    may be replaced by its decimal/hex numeric character reference, a named
    entity, or a percent encoding."""
    return "".join(_capability_char_alt(c) for c in literal)


def _capability_literal_regex(literal: str) -> re.Pattern[str]:
    return re.compile(_capability_literal_pattern(literal), re.IGNORECASE)


# Capability attribute name + optional ``=value`` (any quote style). Used to
# scrub surviving owner/projection-key capability material from comment and
# text content without touching legitimate well-formed start-tag attributes.
_OWNER_CAPABILITY_RE = re.compile(
    _capability_literal_pattern("data-okf-proj-owner")
    + r'(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]*))?',
    re.IGNORECASE,
)
_PROJ_KEY_CAPABILITY_RE = re.compile(
    _capability_literal_pattern("data-okf-projection-key")
    + r'(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]*))?',
    re.IGNORECASE,
)


def _scrub_capability_in_text(
    text: str, nonce_regexes: list[re.Pattern[str]],
) -> str:
    """Remove renderer capability material — owner attrs, projection keys,
    and registered nonce values (literal or entity-encoded) — from a chunk of
    comment or text content, preserving authored prose."""
    text = _OWNER_CAPABILITY_RE.sub("", text)
    text = _PROJ_KEY_CAPABILITY_RE.sub("", text)
    for nr in nonce_regexes:
        text = nr.sub("", text)
    return text


def _scrub_non_tag_capability(
    html: str, nonce_regexes: list[re.Pattern[str]],
) -> str:
    """Scrub owner/key capability material from HTML comments and text
    content, leaving well-formed start-tag attributes (including legitimate
    ``data-okf-projection-key`` keys) intact.

    The walk splits the document into comment regions, tag regions (copied
    verbatim), and text regions. Only comment and text regions are scrubbed,
    so legitimate projection-key attributes on real start tags survive while
    leaked capability material in comments/text/malformed constructs is
    removed.
    """
    out: list[str] = []
    i = 0
    n = len(html)
    while i < n:
        if html.startswith("<!--", i):
            end = html.find("-->", i + 4)
            if end == -1:
                # Unterminated comment — scrub remaining content fail-safe.
                out.append("<!--")
                out.append(_scrub_capability_in_text(html[i + 4:], nonce_regexes))
                return "".join(out)
            out.append("<!--")
            out.append(_scrub_capability_in_text(html[i + 4:end], nonce_regexes))
            out.append("-->")
            i = end + 3
        elif html[i] == "<":
            # Tag (start/end/self-closing) or declaration — copy verbatim.
            gt = html.find(">", i)
            if gt == -1:
                # Unterminated tag-like text (no closing ``>``) — scrub
                # capability material fail-safe instead of copying verbatim,
                # so leaked owner/key/nonce strings in malformed constructs
                # do not survive. (ReviewAll r10: malformed/unterminated
                # tag-like capability text must not bypass the scrub.)
                out.append(_scrub_capability_in_text(html[i:], nonce_regexes))
                return "".join(out)
            out.append(html[i:gt + 1])
            i = gt + 1
        else:
            nxt = html.find("<", i)
            if nxt == -1:
                nxt = n
            out.append(_scrub_capability_in_text(html[i:nxt], nonce_regexes))
            i = nxt
    return "".join(out)


def _decode_text_for_scrub(text: str) -> str:
    """Decode one entity + percent-encoding layer from a TEXT chunk, then
    re-escape angle brackets so structural markup is preserved for the next
    region walk. The raw ``&`` is deliberately left intact so that a nested
    entity-encoded capability fragment (e.g. ``&#97;`` exposed by decoding
    ``&amp;#97;``) stays exposed for the single-layer scrub regex.

    This only runs while contamination is present (the projection is already
    ``complete=False``), so touching escaped prose around attacker-injected
    nonce material is acceptable: nonces are private, and any authored-key
    exemption has already been revoked. Attribute VALUES are never decoded here — they are
    handled by structural parse/serialize (``_scrub_tag_attributes_structural``)
    so quote delimiters can never be decoded into syntax. (ReviewAll r11/r12.)"""
    decoded = _decode_layer(text)
    return decoded.replace("<", "&lt;").replace(">", "&gt;")


def _decode_text_regions_one_layer(html: str) -> str:
    """Decode one encoding layer from text content and malformed/tag-like
    text only — leaving comments, tag markup, and attribute values intact.
    Attribute values are handled separately by structural parse/serialize so
    quote delimiters are never decoded into syntax. (ReviewAll r12 finding 9.)
    """
    out: list[str] = []
    i = 0
    n = len(html)
    while i < n:
        if html.startswith("<!--", i):
            end = html.find("-->", i + 4)
            if end == -1:
                out.append(html[i:])
                return "".join(out)
            out.append(html[i:end + 3])
            i = end + 3
        elif html[i] == "<":
            gt = html.find(">", i)
            if gt == -1:
                # Unterminated tag-like text: decode to expose nested forms.
                out.append(_decode_text_for_scrub(html[i:]))
                return "".join(out)
            # Tag markup copied verbatim; attrs handled structurally elsewhere.
            out.append(html[i:gt + 1])
            i = gt + 1
        else:
            nxt = html.find("<", i)
            if nxt == -1:
                nxt = n
            out.append(_decode_text_for_scrub(html[i:nxt]))
            i = nxt
    return "".join(out)


class _SingleTagAttrParser(HTMLParser):
    """Parse a single start / self-closing tag and capture its attributes.
    End tags and declarations leave ``kind=None`` (copied verbatim by the
    caller). ``HTMLParser`` unescapes attribute values once and parses
    double-quoted, single-quoted, UNQUOTED, and valueless (boolean)
    attributes — the structural basis that replaces regex attribute matching.
    (ReviewAll r12 finding 9.)"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tag = ""
        self.attrs: list[tuple[str, str | None]] = []
        self.kind: str | None = None  # "start" | "startend" | None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.kind = "start"

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.kind = "startend"


def _attr_value_contaminated(
    value: str | None, nonce_regexes: list[re.Pattern[str]],
) -> bool:
    """True if an attribute value decodes to renderer capability material — a
    registered nonce, or the owner/projection-key marker names. ``None``
    (valueless/boolean) attrs are never value-contaminated.

    Uses a GENEROUS decode budget (``_SAFE_FALLBACK_MAX_ITERATIONS``) rather
    than the normal bounded canonicalize: attribute values are small, so
    deep-decoding one value is cheap and termination-safe, and it lets the
    structural scrub DROP attributes carrying deeply-nested-encoded nonce
    material (e.g. 8+ entity layers in ``title="..."``) that the bounded
    whole-document canonicalize cannot reveal. (ReviewAll r12 finding 9.)"""
    if value is None:
        return False
    decoded = value
    for _ in range(_SAFE_FALLBACK_MAX_ITERATIONS):
        nxt = _decode_layer(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    cl = decoded.lower()
    if "data-okf-proj-owner" in cl or "data-okf-projection-key" in cl:
        return True
    return any(nr.search(decoded) for nr in nonce_regexes)


def _scrub_single_tag_attrs(
    tag_text: str, nonce_regexes: list[re.Pattern[str]],
) -> str:
    """Structurally parse one tag's attributes, DROP contaminated attributes,
    and re-serialize with proper quoting. Drops: any attribute named
    ``data-okf-proj-owner`` (pre-finalization marker), and any attribute whose
    value canonicalizes to capability material — including UNQUOTED values.
    Surviving values are re-escaped on emit (``&``/``<``/``>``/``"``) so quote
    delimiters can NEVER be decoded into attribute syntax. The legitimate
    numeric ``data-okf-projection-key`` survives (its value is a clean
    integer). (ReviewAll r12 finding 9.)"""
    parser = _SingleTagAttrParser()
    try:
        parser.feed(tag_text)
        parser.close()
    except Exception:
        return tag_text  # unparsable — leave verbatim (non-tag scrub handles it)
    if parser.kind is None:
        return tag_text  # end tag / declaration — no attributes to scrub
    kept: list[tuple[str, str | None]] = []
    for name, value in parser.attrs:
        if name.lower() == "data-okf-proj-owner":
            continue  # owner marker — always drop
        if _attr_value_contaminated(value, nonce_regexes):
            continue  # contaminated value (any quoting) — drop the attribute
        kept.append((name, value))
    parts = ["<", parser.tag]
    for name, value in kept:
        if value is None:
            parts.append(" " + name)
        else:
            esc = (
                value.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
            )
            parts.append(f' {name}="{esc}"')
    parts.append(" />" if parser.kind == "startend" else ">")
    return "".join(parts)


def _scrub_tag_attributes_structural(
    html: str, nonce_regexes: list[re.Pattern[str]],
) -> str:
    """Walk html; for each tag region, structurally parse and re-serialize its
    attributes, dropping contaminated ones (including unquoted values).
    Comments and text are left untouched for their dedicated scrubbers. Quote
    delimiters are preserved on re-serialization and never decoded into
    syntax. (ReviewAll r12 finding 9.)"""
    out: list[str] = []
    i = 0
    n = len(html)
    while i < n:
        if html.startswith("<!--", i):
            end = html.find("-->", i + 4)
            if end == -1:
                out.append(html[i:])
                return "".join(out)
            out.append(html[i:end + 3])
            i = end + 3
        elif html[i] == "<":
            gt = html.find(">", i)
            if gt == -1:
                # Unterminated tag-like text — leave for the non-tag scrub.
                out.append(html[i:])
                return "".join(out)
            out.append(_scrub_single_tag_attrs(html[i:gt + 1], nonce_regexes))
            i = gt + 1
        else:
            nxt = html.find("<", i)
            if nxt == -1:
                nxt = n
            out.append(html[i:nxt])
            i = nxt
    return "".join(out)


def _strip_contaminated_comments(
    html: str, nonce_regexes: list[re.Pattern[str]],
) -> str:
    """Remove HTML comments whose content canonicalizes (bounded iterative
    decode) to renderer capability material — owner/projection-key attribute
    names or registered nonce values — at ANY encoding depth. Comments are
    inert (never rendered, never parsed as markup by the projection), so
    removing a contaminated comment wholesale is always safe and defeats
    arbitrarily-deep nested encodings that the per-character scrub regex
    cannot match. Clean comments are preserved verbatim. (ReviewAll r11.)"""
    out: list[str] = []
    i = 0
    n = len(html)
    while i < n:
        start = html.find("<!--", i)
        if start == -1:
            out.append(html[i:])
            break
        out.append(html[i:start])
        end = html.find("-->", start + 4)
        if end == -1:
            content = html[start + 4:]
            end_i = n
        else:
            content = html[start + 4:end]
            end_i = end + 3
        canon, _, _ = _canonicalize_iteratively(content)
        cl = canon.lower()
        contaminated = (
            "data-okf-proj-owner" in cl
            or "data-okf-projection-key" in cl
            or any(nr.search(canon) for nr in nonce_regexes)
        )
        if not contaminated:
            out.append(html[start:end_i])
        # Contaminated comments are dropped entirely (safe fallback).
        i = end_i
    return "".join(out)


def _strip_all_comments(html: str) -> str:
    """Remove ALL HTML comments (known-safe fallback). Comments are inert —
    never rendered, never parsed as projection markup — so removing every
    comment guarantees no comment-hidden capability material (at any encoding
    depth) can survive the returned HTML. (ReviewAll r12 blocker.)"""
    out: list[str] = []
    i = 0
    n = len(html)
    while i < n:
        start = html.find("<!--", i)
        if start == -1:
            out.append(html[i:])
            break
        out.append(html[i:start])
        end = html.find("-->", start + 4)
        if end == -1:
            # Unterminated comment — drop the remainder (fail-safe).
            return "".join(out)
        i = end + 3
    return "".join(out)


# Bounded iteration limit for iterative canonicalization. Four iterations
# catch up to quadruple encoding; a browser never decodes more than once in
# text content, but the canonicalizer is deliberately more conservative for
# capability detection. The bound also prevents pathological input from
# causing unbounded re-decoding loops. (Defined once near
# ``_canonicalize_iteratively``; referenced by the scrub below.)
# Generous decode budget for the KNOWN-SAFE fallback (exhaustion path only).
# Large enough to peel any realistic nested-encoding depth so injected
# bytes/capabilities are absent even when the returned HTML is decoded well
# beyond the normal canonicalization budget. Bounded (constant) so the
# exhaustion path still terminates. (ReviewAll r12 blocker.)
_SAFE_FALLBACK_MAX_ITERATIONS = 32


def _scrub_capability_bounded(
    html: str, nonce_regexes: list[re.Pattern[str]],
) -> tuple[str, bool]:
    """Bounded iterative capability scrub covering comments, text, malformed
    constructs, and tag interiors (attributes), using the SAME bounded
    iterative-canonicalization semantics as leak detection so nested encodings
    cannot survive the returned HTML.

    Each pass:
      (a) removes any HTML comment whose content canonicalizes to capability
          material at any depth (``_strip_contaminated_comments``);
      (b) STRUCTURALLY parses each tag's attributes and DROPS contaminated
          ones (owner-marker name, or any value — quoted/unquoted/valueless —
          that canonicalizes to capability material), re-serializing survivors
          with proper quote escaping (``_scrub_tag_attributes_structural``);
      (c) scrubs single-layer (literal / decimal-NCR / hex-NCR / named-entity /
          percent) capability forms from comment interiors, text, and
          malformed constructs (``_scrub_non_tag_capability``);
      (d) decodes ONE encoding layer from TEXT regions (never attribute
          values) so the next pass can scrub newly-exposed nested-encoded
          forms without decoding quote delimiters into syntax.

    Returns ``(scrubbed_html, converged)``. ``converged=False`` (exhaustion)
    means the budget was consumed before the output reached a fixed point; the
    caller applies the known-safe fallback and forces ``complete=False``.
    (ReviewAll r11/r12.)"""
    converged = True
    for _ in range(_CANONICALIZE_MAX_ITERATIONS):
        prev = html
        html = _strip_contaminated_comments(html, nonce_regexes)
        html = _scrub_tag_attributes_structural(html, nonce_regexes)
        html = _scrub_non_tag_capability(html, nonce_regexes)
        for nr in nonce_regexes:
            html = nr.sub("", html)
        html = _decode_text_regions_one_layer(html)
        if html == prev:
            break
    else:
        converged = False
    # Final cleanup pass (no decode) to scrub material exposed by the last
    # decode iteration.
    html = _strip_contaminated_comments(html, nonce_regexes)
    html = _scrub_tag_attributes_structural(html, nonce_regexes)
    html = _scrub_non_tag_capability(html, nonce_regexes)
    for nr in nonce_regexes:
        html = nr.sub("", html)
    return html, converged


def _scrub_capability_known_safe(
    html: str, nonce_regexes: list[re.Pattern[str]],
) -> str:
    """Known-safe capability-free fallback for ANY canonicalization/scrub
    exhaustion. Strips ALL comments (eliminating comment-hidden material at
    any encoding depth) and runs a GENEROUS bounded decode-and-scrub over text
    and tag interiors, so injected bytes/capabilities are absent even when the
    returned HTML is decoded beyond the normal canonicalization budget.

    The output is capability-free under an arbitrarily-large decode because:
      - every comment is gone (no comment can hide material at any depth);
      - every attribute whose value canonicalizes to capability material
        (within the generous budget) is dropped, regardless of quoting;
      - text nonce/marker material is peeled and scrubbed within the generous
        budget.

    Callers still force ``complete=False`` on exhaustion (the input was
    untrustworthy), but the RETURNED html is known-safe. (ReviewAll r12
    blocker.)"""
    html = _strip_all_comments(html)
    for _ in range(_SAFE_FALLBACK_MAX_ITERATIONS):
        prev = html
        html = _scrub_tag_attributes_structural(html, nonce_regexes)
        html = _scrub_non_tag_capability(html, nonce_regexes)
        for nr in nonce_regexes:
            html = nr.sub("", html)
        html = _decode_text_regions_one_layer(html)
        if html == prev:
            break
    # Final pass after the last decode, then a definitive comment strip.
    html = _scrub_tag_attributes_structural(html, nonce_regexes)
    html = _scrub_non_tag_capability(html, nonce_regexes)
    for nr in nonce_regexes:
        html = nr.sub("", html)
    return _strip_all_comments(html)


def _projection_tag_for_kind(kind: str) -> frozenset[str]:
    if kind == "heading": return frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    if kind in {"paragraph", "html"}: return frozenset({"p"})
    if kind == "list-item": return frozenset({"li"})
    if kind == "block-quote": return frozenset({"blockquote"})
    if kind in {"fenced-code", "indented-code"}: return frozenset({"pre", "div"})
    if kind == "table-row": return frozenset({"tr"})
    return frozenset({"hr"}) if kind == "hr" else frozenset()


def _projection_tags_for_block(kind: str, text: str) -> frozenset[str]:
    if _THEMATIC_BREAK.match(text.strip()):
        return frozenset({"hr"})
    # The conservative annotation extractor can classify a paragraph that
    # contains a wikilink pipe as a table row. Final rendered structure wins.
    if kind == "table-row" and not all(
        line.strip().startswith("|") and line.strip().endswith("|")
        for line in text.splitlines() if line.strip()
    ):
        return frozenset({"p"})
    # Footnote definitions are classified as paragraphs by the extractor but
    # rendered as <li> in the footnotes section; accept both tag types.
    if kind == "paragraph" and _FOOTNOTE_DEF_SHAPE.match(text.strip()):
        return frozenset({"p", "li"})
    return _projection_tag_for_kind(kind)


def build_annotation_projection(source: str, body: str, rendered_html: str,
                                owners: list[dict[str, Any]] | None = None,
                                citation_stripped: bool = False, *,
                                _trusted_html: str | None = None) -> tuple[str, dict[str, Any]]:
    """Attach a bounded, revision-qualified canonical-source→DOM block map.

    Production path (*owners* provided): the renderer assigned opaque
    ``data-okf-proj-owner`` nonces to each projectable block's containing
    element **before** the plugin ran. This finalizer verifies that every
    eligible registered owner survives the plugin exactly once with exact
    visible text, replaces verified markers with opaque numeric
    ``data-okf-projection-key`` attributes, strips forged keys, and emits the
    block-segment source map from the IR (not post-hoc fuzzy matching).

    Unknown/duplicate/copied/invented owner nonces and forged numeric keys all
    fail closed. Any pre-finalization ``data-okf-projection-key`` attribute
    (valued, valueless, or any case form) is stripped AND forces
    ``complete=False``. HTML5 tree-construction repair constructs that a
    browser would handle differently from the stdlib ``HTMLParser`` (table
    foster parenting, implied list/container closure, formatting
    reconstruction, foreign content) also force ``complete=False`` because
    textContent extraction per owner nonce would be unreliable. So does any
    element outside ``_PROJECTION_MODELED_TAGS`` that is, encloses or sits
    inside an owner, any table part outside its required parent, and any
    HTML5 tokenization difference the returned-output scan refuses (malformed
    comments, NUL, owned CR or divergent numeric references). Other unmarked
    plugin-added semantics elsewhere are ignored.
    """
    from .markdown_blocks import CANONICAL_BLOCK_IR_VERSION, find_owner_markers, table_row_contexts

    offset = _body_offset(source, body)
    blocks = extract_blocks(source)
    table_contexts = table_row_contexts(blocks)
    max_records = 10_000
    limited = blocks[:max_records]

    # Build canonical records for every block.
    records: list[dict[str, Any]] = []
    _citation_section = False
    for block in limited:
        thematic_break = bool(_THEMATIC_BREAK.match(block.text.strip()))
        bid = ("hr:" + block.id.split(":", 1)[-1]) if thematic_break else block.id
        in_body = offset is not None and block.end > offset and block.start <= len(source)
        tags = _projection_tags_for_block(block.kind, block.text)
        table_context = table_contexts.get(block.start)
        if block.kind == "table-row":
            tags = frozenset({"tr" if table_context else "p"})
        separator_gap = bool(table_context and table_context[1])
        if block.kind == "heading":
            heading_text = block.text.strip().lstrip("#").strip()
            _citation_section = bool(citation_stripped and heading_text.lower() == "citations") if heading_text else False
        eligible = in_body and tags and not separator_gap and not _citation_section
        records.append({
            "block_id": bid,
            "source_start": block.start,
            "source_end": block.end,
            "heading_path": list(block.heading_path),
            "dom_key": None,
            "segments": [],
            "_eligible": eligible,
            "_expected_tags": tags,
            "_block": block,
        })

    final_html = rendered_html

    if owners is None or offset is None:
        for r in records:
            r.pop("_eligible", None)
            r.pop("_expected_tags", None)
            r.pop("_block", None)
        complete = offset is not None and not any(
            r.get("_eligible") for r in records
        ) if owners is None else False
        return final_html, {
            "schema": 1, "resolver_version": 2,
            "block_ir_version": CANONICAL_BLOCK_IR_VERSION,
            "concept_content_rev": concept_content_rev(source),
            "offset_unit": "unicode-code-point",
            "complete": len(blocks) <= max_records and complete,
            "blocks": [{k: v for k, v in r.items() if not k.startswith("_")} for r in records],
        }

    # --- Owner-marker finalization (production path) ---

    _ABSENT = object()

    # 1. Structurally parse ALL start tags with strict nesting validation.
    #    HTMLParser is NOT a browser HTML5 parser — it doesn't auto-close
    #    tags or repair misnesting. We detect constructs a browser WOULD
    #    repair (p auto-closed by block elements, mismatched end tags) and
    #    fail closed because textContent extraction would be unreliable.
    #    These are the HTML5 "in body" start tags that close a p element in
    #    button scope.
    _P_AUTOCLOSE = frozenset({
        "address", "article", "aside", "blockquote", "center", "dd",
        "details", "dialog", "dir", "div", "dl", "dt", "fieldset",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hgroup", "hr", "li", "listing", "main",
        "menu", "nav", "ol", "p", "plaintext", "pre", "search", "section",
        "summary", "table", "ul", "xmp",
    })

    class _TagCollector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.tags: list[dict[str, Any]] = []
            self._cursor = 0
            self.nesting_valid = True
            self._stack: list[tuple[str, bool]] = []  # (tag, is_owner)
            self.unclosed_owner = False
            # HTML5 tree-construction repair detected (text relocation risk).
            self.html5_repair = False
            # The previous token was a <pre> start tag.
            self._after_pre = False
            # Where each open noscript's raw text ends in a scripting browser.
            self._noscript_ends: list[int] = []
            self._line_starts = [0] + [m.end() for m in re.finditer("\n", rendered_html)]

        def _offset(self) -> int:
            line, col = self.getpos()
            return self._line_starts[line - 1] + col

        def _process(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            raw = self.get_starttag_text() or ""
            tag_l = tag.lower()
            owner_entries = [v for k, v in attrs if k.lower() == "data-okf-proj-owner"]
            proj_entries = [v for k, v in attrs if k.lower() == "data-okf-projection-key"]
            owner_count = len(owner_entries)
            has_valueless_owner = any(v is None for v in owner_entries)
            owner_val = owner_entries[-1] if owner_entries else _ABSENT
            has_valueless_key = any(v is None for v in proj_entries)
            proj_val = proj_entries[-1] if proj_entries else _ABSENT
            pos = rendered_html.find(raw, self._cursor) if raw else -1
            if pos >= 0:
                self._cursor = pos + len(raw)
            self.tags.append({
                "tag": tag_l,
                "raw": raw,
                "position": pos,
                "owner": owner_val,
                "owner_count": owner_count,
                "has_valueless_owner": has_valueless_owner,
                "proj_key": proj_val,
                "has_valueless_key": has_valueless_key,
            })

        def _p_in_button_scope(self) -> bool:
            for tag, _owner in reversed(self._stack):
                if tag == "p":
                    return True
                if tag in _HTML5_BUTTON_SCOPE_BARRIERS:
                    return False
            return False

        def _nearest_table_context(self) -> str | None:
            """Return nearest table-context ancestor (table/thead/tbody/tfoot/
            colgroup/tr) if in a foster-parenting position; None if inside a
            cell/caption or outside any table."""
            for tag, _owner in reversed(self._stack):
                if tag in _HTML5_TABLE_CELL_TAGS:
                    return None
                if tag in _HTML5_TABLE_FOSTER_CTX:
                    return tag
            return None

        def _detect_html5_repair(self, tag_l: str) -> None:
            """Flag constructs a browser's HTML5 tree construction would repair
            differently from stdlib HTMLParser (text relocation risk)."""
            # Foreign content: svg/math have fundamentally different rules
            # (case-sensitive tags, CDATA, non-void self-closing, etc.).
            if tag_l in _HTML5_FOREIGN_ROOTS:
                self.html5_repair = True
                return
            # Table foster parenting: a non-table-content element inside
            # table/thead/tbody/tfoot/colgroup/tr is foster-parented
            # (relocated before the table) by the browser.
            ctx = self._nearest_table_context()
            if ctx is not None:
                valid = _HTML5_TABLE_VALID_DIRECT.get(ctx, frozenset())
                if tag_l not in valid:
                    self.html5_repair = True
                    return
            # Implied end-tag closure: li/dd/dt/td/th/tr/option/optgroup
            # auto-close a still-open sibling of the same group.
            rule = _HTML5_IMPLIED_CLOSER.get(tag_l)
            if rule is not None:
                closes, barriers = rule
                for open_tag, _owner in reversed(self._stack):
                    if open_tag in barriers:
                        break
                    if open_tag in closes:
                        self.html5_repair = True
                        return
            # Formatting reconstruction (adoption agency): an open formatting
            # element (<b>, <i>, <a>, ...) spanning a block-level boundary
            # causes the browser to clone/reconstruct it.
            if tag_l in _HTML5_BLOCK_TAGS:
                for open_tag, _owner in reversed(self._stack):
                    if open_tag in _HTML5_BLOCK_TAGS:
                        break
                    if open_tag in _HTML5_FORMATTING_TAGS:
                        self.html5_repair = True
                        return

        def _in_owner(self) -> bool:
            return any(is_owner for _tag, is_owner in self._stack)

        def _detect_unmodeled(self, tag_l: str, has_owner: bool) -> None:
            """Fail closed on a start tag that is, encloses or sits inside a
            keyed owner unless admission models its tree construction.

            Every such element must be allowlisted, and the allowlisted
            headings, anchors and table parts must nest where the parser keeps
            them. Other plugin elements elsewhere cannot move owner text,
            but a misplaced table part can be an owner's ancestor, or close
            the cell an owner follows it in, so table parts are checked
            everywhere."""
            open_tags = [tag for tag, _owner in self._stack]
            if tag_l in _HTML5_TABLE_PART_PARENTS and (
                    not open_tags or open_tags[-1] not in _HTML5_TABLE_PART_PARENTS[tag_l]):
                self.html5_repair = True
            if not has_owner and not self._in_owner():
                return
            if tag_l not in _PROJECTION_MODELED_TAGS or (
                    has_owner and not _PROJECTION_MODELED_TAGS.issuperset(open_tags)):
                self.html5_repair = True
            # A heading start pops an open heading; an anchor start closes an
            # open anchor (adoption agency). Both are refused anywhere above.
            elif tag_l in _HTML5_HEADINGS and not _HTML5_HEADINGS.isdisjoint(open_tags):
                self.html5_repair = True
            elif tag_l == "a" and "a" in open_tags:
                self.html5_repair = True

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            tag_l = tag.lower()
            has_owner = any(k.lower() == "data-okf-proj-owner" for k, _ in attrs)
            self._after_pre = False
            self._detect_p_autoclose(tag_l)
            self._detect_html5_repair(tag_l)
            self._detect_unmodeled(tag_l, has_owner)
            if tag_l == "noscript":
                # Scripting browsers read noscript content as raw text up to
                # the first </noscript, even inside a comment or attribute
                # value; the lexical end tag must be that one.
                end = _NOSCRIPT_RAW_TEXT_END.search(
                    rendered_html, self._offset() + len(self.get_starttag_text() or ""))
                self._noscript_ends.append(end.start() if end else -1)
            if tag_l not in _OwnerTextScanner._VOID:
                self._stack.append((tag_l, has_owner))
            self._process(tag, attrs)
            # The parser drops a line feed that directly follows <pre>.
            self._after_pre = tag_l == "pre"

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            # Self-closing tags (e.g. <svg/>) still need repair detection —
            # a browser parses <svg/> in the SVG namespace, which HTMLParser
            # cannot model. Foster-parenting/implied-closure checks also apply
            # because the self-closing form may still be in a repair context.
            # HTML5 ignores the slash, so <hr/> or <div/> also closes a p.
            tag_l = tag.lower()
            self._after_pre = False
            self._detect_p_autoclose(tag_l)
            self._detect_html5_repair(tag_l)
            self._detect_unmodeled(
                tag_l, any(k.lower() == "data-okf-proj-owner" for k, _ in attrs))
            self._process(tag, attrs)

        def _detect_p_autoclose(self, tag_l: str) -> None:
            # Detect <p> auto-closing (browser HTML5 repair): an open p in
            # button scope closes here even through intervening inline
            # elements, moving the following text out of the paragraph.
            if tag_l in _P_AUTOCLOSE and self._p_in_button_scope():
                self.nesting_valid = False

        def handle_endtag(self, tag: str) -> None:
            tag_l = tag.lower()
            self._after_pre = False
            if tag_l == "noscript" and (
                    not self._noscript_ends or self._noscript_ends.pop() != self._offset()):
                self.html5_repair = True
            if tag_l in _OwnerTextScanner._VOID:
                return
            if not self._stack or self._stack[-1][0] != tag_l:
                if tag_l in [s[0] for s in self._stack]:
                    while self._stack and self._stack[-1][0] != tag_l:
                        if self._stack[-1][1]:
                            self.unclosed_owner = True
                        self._stack.pop()
                    if self._stack:
                        self._stack.pop()
                    else:
                        self.nesting_valid = False
                else:
                    self.nesting_valid = False
            else:
                self._stack.pop()

        def _text(self, value: str) -> None:
            after_pre, self._after_pre = self._after_pre, False
            # A leading line feed right after <pre> (CR normalizes to LF) is
            # dropped by the parser but credited by the lexical scanners.
            if after_pre and value[:1] in ("\n", "\r") and self._in_owner():
                self.html5_repair = True
            # Foster-parented text: non-whitespace text directly inside a
            # table-context element is relocated by the browser. Only HTML
            # ASCII whitespace stays; U+00A0 and other spaces are text.
            if (value.strip(_HTML_ASCII_WHITESPACE)
                    and self._nearest_table_context() is not None):
                self.html5_repair = True

        def handle_data(self, data: str) -> None:
            self._text(data)

        # References resolve to character tokens: the same checks apply to
        # their decoded text.
        def handle_entityref(self, name: str) -> None:
            self._text(_html.unescape(f"&{name};"))

        def handle_charref(self, name: str) -> None:
            self._text(_html.unescape(f"&#{name};"))

        def handle_comment(self, data: str) -> None:
            self._after_pre = False

        def close(self) -> None:
            super().close()
            for _tag, is_owner in self._stack:
                if is_owner:
                    self.unclosed_owner = True

    collector = _TagCollector()
    parse_error = False
    try:
        collector.feed(final_html)
        collector.close()
    except Exception:
        parse_error = True
    all_tags = collector.tags
    nesting_ok = collector.nesting_valid and not collector.unclosed_owner and not parse_error
    # Any pre-finalization data-okf-projection-key attribute (any case, valued
    # or valueless) is forged input. It is stripped below but must ALSO force
    # incomplete projection — the documented fail-closed contract. (ReviewAll
    # r7: "forged projection keys contradict the fail-closed contract".)
    preexisting_key = any(t["proj_key"] is not _ABSENT for t in all_tags)
    html5_repair = collector.html5_repair

    # 2. Identify owner markers. A tag with != exactly 1 owner occurrence
    #    (any case/quote/order, including duplicate identical values) is invalid.
    post_markers = []
    for t in all_tags:
        if t["owner"] is _ABSENT:
            continue
        owner_val = t["owner"]
        is_valueless = t.get("has_valueless_owner", False) or owner_val is None
        nonce_str = owner_val if owner_val is not None else ""
        owner_count = t.get("owner_count", 0)
        post_markers.append({
            "nonce": nonce_str,
            "position": t["position"],
            "tag_text": t["raw"],
            "tag": t["tag"],
            "valueless": is_valueless,
            "owner_count_invalid": owner_count != 1,
        })
    registered = {o["nonce"] for o in owners}
    nonce_counts: dict[str, int] = {}
    nonce_to_marker: dict[str, dict[str, Any]] = {}
    for pm in post_markers:
        nonce_counts[pm["nonce"]] = nonce_counts.get(pm["nonce"], 0) + 1
        if pm["nonce"] not in nonce_to_marker:
            nonce_to_marker[pm["nonce"]] = pm
    # Forged = unknown nonce, valueless, wrong owner count, or nesting invalid.
    forged = [pm for pm in post_markers
              if pm["nonce"] not in registered
              or pm["valueless"]
              or pm["owner_count_invalid"]]
    forged_nonces = {pm["nonce"] for pm in forged}
    duplicated = {n for n, c in nonce_counts.items() if c > 1 and n in registered}

    text_scanner = _OwnerTextScanner()
    try:
        text_scanner.feed(final_html)
        text_scanner.close()
    except Exception:
        text_scanner.text = {}

    # 3. Match owners to canonical blocks by containment.
    used_indices: set[int] = set()

    def _find_containing(src_start: int) -> int | None:
        for bi, block in enumerate(limited):
            if bi in used_indices:
                continue
            if block.start <= src_start < block.end:
                return bi
        return None

    dom_key_counter = 0
    mapped_owners: dict[str, int] = {}

    nesting_failed = not nesting_ok and bool(post_markers)

    for owner in owners:
        src_start = owner["start"] + offset
        idx = _find_containing(src_start)
        if idx is None:
            continue
        used_indices.add(idx)
        record = records[idx]

        nonce = owner["nonce"]
        has_error = (nonce in duplicated or nonce in forged_nonces
                     or nonce not in nonce_counts or nesting_failed)

        if has_error:
            continue

        expected = owner["expected_visible"]
        actual = text_scanner.text.get(nonce, "")
        is_hr = record["block_id"].startswith("hr:")

        # Indeterminate visible mapping (None) stays unmappable, not empty match.
        if expected is None:
            continue
        # Owners whose expected AND rendered visible text are empty carry no
        # selectable text. Only thematic breaks (hr:*) are valid keyed
        # zero-segment records; every other empty rendered block is
        # non-selectable. Strip its owner marker (step 4 strips every owner
        # marker) WITHOUT assigning a DOM projection key, leave dom_key null /
        # segments empty, and mark it non-eligible so it is not counted as an
        # unmapped block. ``complete`` stays true because no selectable text
        # was omitted. A genuine mismatch (expected "" but actual non-empty)
        # still falls through to the match check below and fails closed.
        if expected == "" and actual == "" and not is_hr:
            record["_eligible"] = False
            continue
        if actual == expected or (is_hr and expected == "" and actual == ""):
            marker = nonce_to_marker.get(nonce)
            actual_tag = marker.get("tag", "") if marker else ""
            expected_tags = record["_expected_tags"]
            if not actual_tag or actual_tag not in expected_tags:
                continue
            dom_key_counter += 1
            mapped_owners[nonce] = dom_key_counter
            record["dom_key"] = dom_key_counter
            record["_expected_visible"] = expected
            record["segments"] = [
                {
                    "dom_start": s["dom_start"],
                    "dom_end": s["dom_end"],
                    "source_start": s["source_start"] + offset,
                    "source_end": s["source_end"] + offset,
                }
                for s in owner["segments"]
            ]
            mapped_block = records[idx]["_block"]
            for other in records:
                if other is record:
                    continue
                if other.get("_eligible") and other["dom_key"] is None:
                    ob = other["_block"]
                    if mapped_block.start <= ob.start and ob.end <= mapped_block.end:
                        other["_eligible"] = False

    # A parallel key-only detection view masks exact proven text positions and
    # the numeric attributes emitted below, never whole owners or global counts.
    # Nonce/marker detection and exhaustion ALWAYS use the unmasked output.
    key_view = rendered_html
    if nesting_ok and not html5_repair and not forged and not duplicated and not preexisting_key:
        for start, end in reversed(_authored_key_ranges(
                _trusted_html, rendered_html, owners, mapped_owners, source, offset)):
            key_view = key_view[:start] + " " * (end - start) + key_view[end:]

    # 4. Structural start-tag rewrite (reverse position order).
    for st in reversed(all_tags):
        pos = st["position"]
        if pos < 0:
            continue
        raw = st["raw"]
        new_raw = raw
        emitted = ""
        if st["proj_key"] is not _ABSENT or st.get("has_valueless_key"):
            new_raw = re.sub(
                r'\s+data-okf-projection-key(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+))?',
                "", new_raw, flags=re.IGNORECASE,
            )
        if st["owner"] is not _ABSENT or st.get("has_valueless_owner"):
            owner_val = st["owner"]
            nonce_str = owner_val if owner_val is not _ABSENT and owner_val is not None else ""
            new_raw = re.sub(
                r'\s+data-okf-proj-owner(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+))?',
                "", new_raw, flags=re.IGNORECASE,
            )
            if nonce_str in mapped_owners:
                key = mapped_owners[nonce_str]
                emitted = f' data-okf-projection-key="{key}"'
                if new_raw.endswith("/>"):
                    new_raw = new_raw[:-2].rstrip() + f' data-okf-projection-key="{key}" />'
                elif new_raw.endswith(">"):
                    new_raw = new_raw[:-1] + f' data-okf-projection-key="{key}">'
        if new_raw != raw:
            final_html = final_html[:pos] + new_raw + final_html[pos + len(raw):]
            masked_raw = new_raw
            if emitted:
                # The last occurrence is the attribute we just appended, not
                # any identical spelling inside an attacker-controlled value.
                at = new_raw.rindex(emitted)
                masked_raw = new_raw[:at] + " " * len(emitted) + new_raw[at + len(emitted):]
            key_view = key_view[:pos] + masked_raw + key_view[pos + len(raw):]

    # 4b. Marker-leak guard: if ANY owner-like or projection-key substring
    #     survives after rewrite, NEVER accept output as complete. Detection
    #     runs against a BOUNDED ITERATIVE CANONICALIZED view that peels
    #     successive HTML-entity (named, decimal NCR, hex NCR — including
    #     ``&hyphen;``) and percent-encoding layers, so double-encoded
    #     entities (``&amp;#45;`` → ``&#45;`` → ``-``) and percent-encoded
    #     capability material (``%64%61%74%61`` → ``data``) are caught in
    #     addition to single-pass entity-encoded forms. The canonicalizer
    #     also reports convergence/exhaustion: if the budget is exhausted
    #     (encoding deeper than ``_CANONICALIZE_MAX_ITERATIONS``), detection
    #     cannot prove the output capability-free and MUST force the safe
    #     fallback (``complete=False``). (ReviewAll r10/r11.)
    canonical_html, _pre_conv, pre_exhausted = _canonicalize_iteratively(final_html)
    marker_leak = ("data-okf-proj-owner" in canonical_html.lower())

    # Owner nonce values must not survive in comments, text, or malformed
    # constructs that the structural start-tag rewrite does not reach.
    # After step 4 all legitimate nonces are replaced by numeric projection
    # keys; any surviving nonce string (literal, entity-encoded, percent-
    # encoded, or double-encoded) is a leak.
    registered_nonces = {o["nonce"] for o in owners if o.get("nonce")}
    leaking_nonces = {
        nonce for nonce in registered_nonces
        if nonce and nonce in canonical_html
    }
    nonce_leak = bool(leaking_nonces)

    # Only exact emitted attributes and source-proven text are masked. Any
    # remaining spelling, at any bounded encoding depth, is contamination.
    canonical_keys, _, _ = _canonicalize_iteratively(key_view)
    key_leak = "data-okf-projection-key" in canonical_keys.lower()

    def _returned_text_matches() -> bool:
        # Source-derived expected_visible remains authoritative after rewrite
        # and scrub. Checking only the original owner text is insufficient.
        # Plugin-added elements left open at end of document are ignored only
        # while none of them is or wraps a keyed element.
        returned = _ProjectionTextPositions(final_html, "data-okf-projection-key",
                                            allow_trailing_open=True)
        keyed = [r for r in records if r["dom_key"] is not None]
        if not returned.valid or set(returned.elements) != {str(r["dom_key"]) for r in keyed}:
            return False
        for r in keyed:
            elements = returned.elements[str(r["dom_key"])]
            if len(elements) != 1 or elements[0]["text"] != r["_expected_visible"]:
                return False
        return True

    verified_html = final_html
    final_text_ok = _returned_text_matches()
    revoke_authored = (nonce_leak or marker_leak or key_leak or pre_exhausted
                      or not nesting_ok or html5_repair or bool(forged or duplicated)
                      or preexisting_key or not final_text_ok
                      or any(r["_eligible"] and r["dom_key"] is None for r in records))

    # 4c. Remove contaminated owner/key material from the returned HTML.
    #     Detection already forced ``complete=False`` above; the contaminated
    #     material must additionally NOT survive the returned ``final_html`` —
    #     including comments, text, malformed/unterminated constructs, and tag
    #     interiors — while authored ordinary prose remains intact. The scrub
    #     is BOUNDED and ITERATIVE, applying the same canonicalization
    #     semantics as detection: each pass strips contaminated comments,
    #     STRUCTURALLY parses tag attributes and DROPS contaminated ones
    #     (quoted/unquoted/valueless, quote delimiters never decoded into
    #     syntax), scrubs single-layer forms from text/comments, then decodes
    #     one TEXT layer to expose nested-encoded forms. (ReviewAll r10/r11/
    #     r12 finding 9.)
    scrub_converged = True
    if revoke_authored:
        nonce_regexes = [_capability_literal_regex(n) for n in leaking_nonces]
        final_html, scrub_converged = _scrub_capability_bounded(
            final_html, nonce_regexes,
        )

    # 4d. Post-scrub canonicalization verification + KNOWN-SAFE fallback.
    #     Re-canonicalize the scrubbed output and check that no capability
    #     material survives in ANY encoded form. On ANY canonicalization/scrub
    #     EXHAUSTION (pre/post/scrub) or residual contamination, replace
    #     ``final_html`` with a KNOWN-SAFE capability-free form: ALL comments
    #     stripped (defeats comment-hidden material at any depth) and a generous
    #     decode-and-scrub over text/tag-interiors so injected bytes are absent
    #     even when the returned HTML is decoded beyond the normal budget. ALL
    #     registered nonces are used for the known-safe scrub because exhaustion
    #     means the bounded canonicalize may not have revealed every leaking
    #     nonce. The projection is still forced ``complete=False`` (input was
    #     untrustworthy), but the RETURNED html is known-safe. (ReviewAll r12
    #     blocker.)
    canonical_final, _post_conv, post_exhausted = _canonicalize_iteratively(final_html)
    residual_marker = "data-okf-proj-owner" in canonical_final.lower()
    residual_nonce = any(
        n and n in canonical_final for n in registered_nonces
    )
    residual_key = revoke_authored and (
        canonical_final.lower().count("data-okf-projection-key")
        != dom_key_counter
    )
    any_exhaustion = pre_exhausted or post_exhausted or not scrub_converged
    if any_exhaustion or residual_marker or residual_nonce or residual_key:
        safe_regexes = [_capability_literal_regex(n) for n in registered_nonces if n]
        final_html = _scrub_capability_known_safe(final_html, safe_regexes)
        canonical_final, _, _ = _canonicalize_iteratively(final_html)
        residual_marker = "data-okf-proj-owner" in canonical_final.lower()
        residual_nonce = any(
            n and n in canonical_final for n in registered_nonces
        )
        residual_key = (
            canonical_final.lower().count("data-okf-projection-key")
            != dom_key_counter
        )
        residual_leak = residual_marker or residual_nonce or residual_key
    else:
        residual_leak = False

    # 5. Completeness: every eligible block must be mapped, keys unique,
    #    no forged/duplicated/leaked markers, nesting valid, no HTML5 repair,
    #    no pre-finalization projection keys, and no exhaustion (the input was
    #    untrustworthy even though the known-safe fallback cleaned the output).
    mapped_keys = [r["dom_key"] for r in records if r["dom_key"] is not None]
    has_eligible_unmapped = any(r["_eligible"] and r["dom_key"] is None for r in records)
    keys_unique = len(mapped_keys) == len(set(mapped_keys))
    # A scrub may change text or structure. Never reuse pre-scrub verification
    # for different returned bytes, and never restore revoked authored text.
    if final_html != verified_html:
        final_text_ok = _returned_text_matches()
    segment_ok = all(
        r["dom_key"] is None or bool(r["segments"]) or r["block_id"].startswith("hr:")
        or r.get("_expected_visible") == ""
        for r in records
    )
    no_errors = (
        not forged and not duplicated and not marker_leak
        and not nonce_leak and not key_leak
        and not parse_error and not html5_repair and not preexisting_key
        and not residual_leak and not any_exhaustion
        and not revoke_authored and final_text_ok
    )

    for r in records:
        r.pop("_eligible", None)
        r.pop("_expected_tags", None)
        r.pop("_expected_visible", None)
        r.pop("_block", None)

    complete = (
        len(blocks) <= max_records
        and not has_eligible_unmapped
        and keys_unique
        and segment_ok
        and no_errors
    )

    projection = {
        "schema": 1,
        "resolver_version": 2,
        "block_ir_version": CANONICAL_BLOCK_IR_VERSION,
        "concept_content_rev": concept_content_rev(source),
        "offset_unit": "unicode-code-point",
        "complete": complete,
        "blocks": records,
    }
    return final_html, projection


# ---------------------------------------------------------------------------
# Chip foreground color (luminance-aware contrast)
# ---------------------------------------------------------------------------

import re as _re_chip


# P2-23: small named-CSS-colour map so palette overrides using bare names
# (``red``, ``navy``, …) get the same luminance-aware fg treatment as
# ``hsl()``. assets.py:_sanitize_css_color already allows the full CSS3
# named set, so without this map ``_chip_fg`` fell through to the white
# default for every named colour (e.g. ``yellow`` chips rendered as
# white-on-yellow). The map is intentionally limited to the high-contrast
# common names; ``transparent`` / ``currentcolor`` stay on the white
# fallback (they have no computable luminance).
_NAMED_CSS_COLORS: dict[str, tuple[int, int, int]] = {
    "aliceblue": (240, 248, 255), "antiquewhite": (250, 235, 215),
    "aqua": (0, 255, 255), "aquamarine": (127, 255, 212),
    "azure": (240, 255, 255), "beige": (245, 245, 220),
    "bisque": (255, 228, 196), "black": (0, 0, 0),
    "blanchedalmond": (255, 235, 205), "blue": (0, 0, 255),
    "blueviolet": (138, 43, 226), "brown": (165, 42, 42),
    "burlywood": (222, 184, 135), "cadetblue": (95, 158, 160),
    "chartreuse": (127, 255, 0), "chocolate": (210, 105, 30),
    "coral": (255, 127, 80), "cornflowerblue": (100, 149, 237),
    "cornsilk": (255, 248, 220), "crimson": (220, 20, 60),
    "cyan": (0, 255, 255), "darkblue": (0, 0, 139),
    "darkcyan": (0, 139, 139), "darkgoldenrod": (184, 134, 11),
    "darkgray": (169, 169, 169), "darkgreen": (0, 100, 0),
    "darkgrey": (169, 169, 169), "darkkhaki": (189, 183, 107),
    "darkmagenta": (139, 0, 139), "darkolivegreen": (85, 107, 47),
    "darkorange": (255, 140, 0), "darkorchid": (153, 50, 204),
    "darkred": (139, 0, 0), "darksalmon": (233, 150, 122),
    "darkseagreen": (143, 188, 143), "darkslateblue": (72, 61, 139),
    "darkslategray": (47, 79, 79), "darkslategrey": (47, 79, 79),
    "darkturquoise": (0, 206, 209), "darkviolet": (148, 0, 211),
    "deeppink": (255, 20, 147), "deepskyblue": (0, 191, 255),
    "dimgray": (105, 105, 105), "dimgrey": (105, 105, 105),
    "dodgerblue": (30, 144, 255), "firebrick": (178, 34, 34),
    "floralwhite": (255, 250, 240), "forestgreen": (34, 139, 34),
    "fuchsia": (255, 0, 255), "gainsboro": (220, 220, 220),
    "ghostwhite": (248, 248, 255), "gold": (255, 215, 0),
    "goldenrod": (218, 165, 32), "gray": (128, 128, 128),
    "green": (0, 128, 0), "greenyellow": (173, 255, 47),
    "grey": (128, 128, 128), "honeydew": (240, 255, 240),
    "hotpink": (255, 105, 180), "indianred": (205, 92, 92),
    "indigo": (75, 0, 130), "ivory": (255, 255, 240),
    "khaki": (240, 230, 140), "lavender": (230, 230, 250),
    "lavenderblush": (255, 240, 245), "lawngreen": (124, 252, 0),
    "lemonchiffon": (255, 250, 205), "lightblue": (173, 216, 230),
    "lightcoral": (240, 128, 128), "lightcyan": (224, 255, 255),
    "lightgoldenrodyellow": (250, 250, 210),
    "lightgray": (211, 211, 211), "lightgreen": (144, 238, 144),
    "lightgrey": (211, 211, 211), "lightpink": (255, 182, 193),
    "lightsalmon": (255, 160, 122), "lightseagreen": (32, 178, 170),
    "lightskyblue": (135, 206, 250),
    "lightslategray": (119, 136, 153),
    "lightslategrey": (119, 136, 153),
    "lightsteelblue": (176, 196, 222), "lightyellow": (255, 255, 224),
    "lime": (0, 255, 0), "limegreen": (50, 205, 50),
    "linen": (250, 240, 230), "magenta": (255, 0, 255),
    "maroon": (128, 0, 0), "mediumaquamarine": (102, 205, 170),
    "mediumblue": (0, 0, 205), "mediumorchid": (186, 85, 211),
    "mediumpurple": (147, 112, 219), "mediumseagreen": (60, 179, 113),
    "mediumslateblue": (123, 104, 238),
    "mediumspringgreen": (0, 250, 154),
    "mediumturquoise": (72, 209, 204),
    "mediumvioletred": (199, 21, 133),
    "midnightblue": (25, 25, 112), "mintcream": (245, 255, 250),
    "mistyrose": (255, 228, 225), "moccasin": (255, 228, 181),
    "navajowhite": (255, 222, 173), "navy": (0, 0, 128),
    "oldlace": (253, 245, 230), "olive": (128, 128, 0),
    "olivedrab": (107, 142, 35), "orange": (255, 165, 0),
    "orangered": (255, 69, 0), "orchid": (218, 112, 214),
    "palegoldenrod": (238, 232, 170), "palegreen": (152, 251, 152),
    "paleturquoise": (175, 238, 238),
    "palevioletred": (219, 112, 147), "papayawhip": (255, 239, 213),
    "peachpuff": (255, 218, 185), "peru": (205, 133, 63),
    "pink": (255, 192, 203), "plum": (221, 160, 221),
    "powderblue": (176, 224, 230), "purple": (128, 0, 128),
    "rebeccapurple": (102, 51, 153), "red": (255, 0, 0),
    "rosybrown": (188, 143, 143), "royalblue": (65, 105, 225),
    "saddlebrown": (139, 69, 19), "salmon": (250, 128, 114),
    "sandybrown": (244, 164, 96), "seagreen": (46, 139, 87),
    "seashell": (255, 245, 238), "sienna": (160, 82, 45),
    "silver": (192, 192, 192), "skyblue": (135, 206, 235),
    "slateblue": (106, 90, 205), "slategray": (112, 128, 144),
    "slategrey": (112, 128, 144), "snow": (255, 250, 250),
    "springgreen": (0, 255, 127), "steelblue": (70, 130, 180),
    "tan": (210, 180, 140), "teal": (0, 128, 128),
    "thistle": (216, 191, 216), "tomato": (255, 99, 71),
    "turquoise": (64, 224, 208), "violet": (238, 130, 238),
    "wheat": (245, 222, 179), "white": (255, 255, 255),
    "whitesmoke": (245, 245, 245), "yellow": (255, 255, 0),
    "yellowgreen": (154, 205, 50),
}


def _relative_luminance_rgb(r: float, g: float, b: float) -> float:
    """WCAG relative luminance for an RGB triple where each channel is in [0, 1]."""
    def _lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast_ratio(lum_fg: float, lum_bg: float) -> float:
    """WCAG contrast ratio between two relative luminances."""
    light, dark = max(lum_fg, lum_bg), min(lum_fg, lum_bg)
    return (light + 0.05) / (dark + 0.05)


def _chip_fg(bg_css: str) -> str:
    """Compute a readable foreground color for a chip with the given CSS background.

    Picks black or white based on which gives the higher WCAG contrast ratio
    against the background (P2-23: replaces the brittle luminance threshold
    of 0.18, which had no margin near the boundary). Handles ``hsl()``,
    ``rgb()``/``rgba()``, ``#hex`` (3/6/8-digit), and the common CSS named
    colours; unparseable values fall back to ``#ffffff``.

    P1-7: hex and ``rgb()``/``rgba()`` inputs are now parsed (previously
    only ``hsl()`` and named colours were, so palette overrides using hex
    like ``#ffff00`` fell through to ``#ffffff`` and produced invisible
    white-on-yellow chips). The parse order and math mirror the JS port
    ``graph.js:_chipFg`` exactly so a palette override yields the same fg
    in the server-rendered concept page and the client-rendered graph view
    (contract-runtime-parity invariant).

    The dark-foreground choice is pure black (``#000000``) rather than a
    softened ``#1a1a2e`` — pure black passes WCAG AA for every hue in the
    auto-palette (verified across 0..360 deg at ``hsl(h, 62%, 48%)``).
    """
    if not isinstance(bg_css, str):
        return "#ffffff"
    s = bg_css.strip()
    rgb: tuple[float, float, float] | None = None
    m = _re_chip.match(r"hsla?\(\s*(\d+(?:\.\d+)?)\s*(?:deg|rad|turn|grad)?\s*,\s*(\d+(?:\.\d+)?)%\s*,\s*(\d+(?:\.\d+)?)%", s)
    if m:
        h = float(m.group(1)) % 360
        sat = float(m.group(2)) / 100.0
        l = float(m.group(3)) / 100.0
        c = (1 - abs(2 * l - 1)) * sat
        x = c * (1 - abs((h / 60) % 2 - 1))
        m_ = l - c / 2
        if h < 60:
            r, g, b = c, x, 0
        elif h < 120:
            r, g, b = x, c, 0
        elif h < 180:
            r, g, b = 0, c, x
        elif h < 240:
            r, g, b = 0, x, c
        elif h < 300:
            r, g, b = x, 0, c
        else:
            r, g, b = c, 0, x
        rgb = (r + m_, g + m_, b + m_)
    else:
        # P1-7: parse rgb()/rgba() (alpha is ignored — luminance is RGB).
        m_rgb = _re_chip.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", s)
        m_hex = _re_chip.match(
            r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$", s, _re_chip.IGNORECASE
        )
        if m_rgb:
            rgb = (
                float(m_rgb.group(1)) / 255.0,
                float(m_rgb.group(2)) / 255.0,
                float(m_rgb.group(3)) / 255.0,
            )
        elif m_hex:
            h = m_hex.group(1)
            if len(h) == 3:
                # Expand shorthand: #abc → #aabbcc.
                h = "".join(ch * 2 for ch in h)
            elif len(h) == 8:
                # Strip alpha (#rrggbbaa) — luminance is RGB-only.
                h = h[:6]
            rgb = (
                int(h[0:2], 16) / 255.0,
                int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0,
            )
        else:
            named = _NAMED_CSS_COLORS.get(s.lower())
            if named is not None:
                rgb = (named[0] / 255.0, named[1] / 255.0, named[2] / 255.0)
    if rgb is None:
        return "#ffffff"
    lum_bg = _relative_luminance_rgb(*rgb)
    # P2-23: pick the higher-contrast foreground instead of using a brittle
    # luminance threshold. Black has luminance 0, white has luminance 1.
    contrast_black = _contrast_ratio(0.0, lum_bg)
    contrast_white = _contrast_ratio(1.0, lum_bg)
    return "#000000" if contrast_black >= contrast_white else "#ffffff"

# ---------------------------------------------------------------------------
# Bundle → graph JSON
# ---------------------------------------------------------------------------

# P1-3 (iter-1): matches RFC-3986-style scheme prefixes (e.g. "javascript:",
# "data:", "mailto:") so _graph_governed_keys can distinguish URL-like values
# (which _safe_url must vet) from plain-text values (kept for display).
_LINK_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def _graph_governed_keys(concept: Concept) -> dict[str, list]:
    """Extract governed frontmatter keys (§7) for the graph view.

    Mirrors :func:`_render_governed_keys` (concept page) but returns a
    JSON-safe dict so the data ships to the client (graph.js renders it in
    the detail panel). Every key is always present, defaulting to an empty
    list when the frontmatter key is absent or malformed — the client hides
    empty sections, so absence is graceful (fail-closed).

    URLs in ``provenance[].source`` and ``citations[].url`` are run through
    :func:`_safe_url` so a ``javascript:``/``data:`` value can't XSS the
    graph view (graph.js assigns them to ``a.href``; graph.js ALSO guards
    with an http(s)-only check, so this is defense-in-depth). A value that
    looks like a URL (has a scheme) but is rejected by ``_safe_url`` is
    blanked; plain-text values with no scheme are kept for display.

    ``relations`` is read from the frontmatter (typed-relation declarations)
    so the detail panel can show the relation type + target even when the
    target concept is not in the bundle (unresolved). Resolved edges are
    already shipped separately in ``edges``.
    """
    from .viewer.markdown import _safe_url

    def _safe_list(v: Any) -> list:
        return list(v) if isinstance(v, (list, tuple)) else []

    def _sanitize_linkable(val: str) -> str:
        """Return a safe href for allowed URLs, plain text for non-URL
        values, or '' for URL-like-but-dangerous schemes."""
        if not val:
            return ""
        safe = _safe_url(val)
        if safe:
            return safe
        # Not an allowed URL. Keep it as display text only when it has no
        # scheme (e.g. "internal memo"); blank scheme-bearing values that
        # _safe_url rejected (javascript:, data:, ...).
        return "" if _LINK_SCHEME_RE.match(val) else val

    fm = concept.frontmatter

    # aliases — tolerate strings or {label, discoverable} dicts.
    aliases = alias_labels(fm.get("aliases"))

    # entities — tolerate bare strings or {id,label,kind,aliases} dicts (§7.2)
    entities_raw = _safe_list(fm.get("entities"))
    entities: list[dict[str, Any]] = []
    for ent in entities_raw:
        if isinstance(ent, str) and ent.strip():
            entities.append({"id": "", "label": str(ent).strip(), "kind": "", "aliases": []})
        elif isinstance(ent, dict):
            ent_aliases = _safe_list(ent.get("aliases"))
            entities.append({
                "id": str(ent.get("id", "") or ""),
                "label": str(ent.get("label", "") or ""),
                "kind": str(ent.get("kind", "") or ""),
                "aliases": [str(a).strip() for a in ent_aliases if isinstance(a, str) and str(a).strip()],
            })

    # provenance — [{source,note,timestamp}] with sanitized source URL
    prov_raw = _safe_list(fm.get("provenance"))
    provenance: list[dict[str, str]] = []
    for prov in prov_raw:
        if not isinstance(prov, dict):
            continue
        provenance.append({
            "source": _sanitize_linkable(str(prov.get("source", "") or "")),
            "note": str(prov.get("note", "") or ""),
            "timestamp": str(prov.get("timestamp", "") or ""),
        })

    # citations — [{id,text,url}] with sanitized url
    cit_raw = _safe_list(fm.get("citations"))
    citations: list[dict[str, str]] = []
    for cit in cit_raw:
        if not isinstance(cit, dict):
            continue
        citations.append({
            "id": str(cit.get("id", "") or ""),
            "text": str(cit.get("text", "") or ""),
            "url": _sanitize_linkable(str(cit.get("url", "") or "")),
        })

    # relations — typed-relation declarations [{type,target,via}]
    rel_raw = _safe_list(fm.get("relations"))
    relations: list[dict[str, str]] = []
    for rel in rel_raw:
        if not isinstance(rel, dict):
            continue
        relations.append({
            "type": str(rel.get("type", "related") or "related"),
            "target": str(rel.get("target", "") or ""),
            "via": str(rel.get("detail", "") or ""),
        })

    return {
        "aliases": aliases,
        "entities": entities,
        "provenance": provenance,
        "citations": citations,
        "relations": relations,
    }


_GRAPH_GROUPING_KEYS = (
    "graph_cluster",
    "source_system",
    "project",
    "section",
    "import_batch",
    "redmine_project",
)


def _graph_grouping_metadata(concept: Concept) -> dict[str, str]:
    """Return a small allowlist of graph grouping metadata.

    Unknown frontmatter is legal and preserved, but graph JSON is a rendered
    read surface. Do not expose arbitrary keys here; only ship generic grouping
    fields that are useful across imported or mixed-source bundles.
    """
    out: dict[str, str] = {}
    fm = concept.frontmatter
    for key in _GRAPH_GROUPING_KEYS:
        value = fm.get(key)
        if isinstance(value, (list, dict)):
            continue
        text = str(value or "").strip()
        if text:
            out[key] = text[:160]
    return out


def build_graph_data(
    bundle: Bundle,
    *,
    name: str | None = None,
    comment_summary: CommentSummaryV1 | None = None,
) -> dict[str, Any]:
    """Serialise a Bundle into the JSON shape consumed by the viewer.

    Shape (consumed by ``viewer/static/graph.js``):
        {
          "name": str,
          "nodes": [{data: {id, label, type, description, resource, tags,
                            color, size, degree, unresolved_count,
                            aliases, entities, provenance, citations,
                            relations,
                            metadata, graph_cluster, source_system}}],
          "edges": [{data: {id, source, target, label, multiplicity,
                             primary_relation, relations}}],
          "external": [{source, target_raw, label}],
          "bodies": {id: markdown_body},
          "types": [str],
          "palette": {type: css_color},
          "backlinks": {target_id: [source_id, ...]},
        }

    The §7 governed keys (P1-3 iter-1) are always present per node,
    defaulting to empty lists when the frontmatter key is absent.

    Phase 3 graph visual language:
      * ``size`` is DEGREE-driven (eased sqrt ramp 24..52), replacing the
        prior body-length proxy. The same dangling-endpoint invariant as
        the edge emission loop is applied: only edges whose source AND
        target are loaded concepts contribute to a node's degree.
      * ``degree`` exposes the same in-bundle degree the size hint
        reflects (sum of outgoing + backlinks list lengths over deduped
        edges — both directions counted separately, mirroring the
        client's ``degreeOf``). Static/live/single-file all see identical
        values so the client hub-halo / orphan-shelf computations are
        contract-stable across render paths.
      * ``unresolved_count`` is a deterministic count of
        ``Graph.unresolved`` entries whose source is this concept. It does
        NOT introduce new product semantics — broken links are already
        defined as ``Graph.unresolved`` at the model layer (SPEC §5.3) —
        so the count is well-defined across static/live/single-file render
        paths (all three call ``bundle.graph()`` on the same Bundle).
    """
    from .retrieval import bundle_revision

    graph = bundle.graph()
    palette = resolve_palette(bundle)
    types = sorted(t for t in bundle.types() if t)

    comment_counts: dict[str, int] | None = None
    if comment_summary is not None:
        expected_keys = {"schema", "semantics", "counts", "summary_revision"}
        if (not isinstance(comment_summary, dict)
                or set(comment_summary) != expected_keys
                or comment_summary.get("schema") != 1
                or comment_summary.get("semantics") != "active-comment-row"
                or not isinstance(comment_summary.get("counts"), dict)
                or not isinstance(comment_summary.get("summary_revision"), str)
                or re.fullmatch(r"[0-9a-f]{64}", comment_summary["summary_revision"]) is None):
            raise ValueError("invalid CommentSummaryV1")
        comment_counts = comment_summary["counts"]
        concept_names = {concept_id_to_str(c.id) for c in bundle.concepts.values()}
        if any(
            not isinstance(key, str) or key not in concept_names
            or type(value) is not int or value < 0
            for key, value in comment_counts.items()
        ):
            raise ValueError("invalid CommentSummaryV1 counts")
        if comment_summary["summary_revision"] != canonical_comment_summary_revision(
            comment_counts
        ):
            raise ValueError("invalid CommentSummaryV1 revision")

    # Phase 3: per-node degree is computed below from the SAME deduped-edge
    # loop that builds ``edges``/``backlinks``, so the server-side size hint
    # and the client-side ``degreeOf`` (sum of outgoing + backlinks list
    # lengths over deduped edges — both directions counted separately)
    # agree by construction (contract-runtime-parity across
    # static/live/single-file).
    _concept_ids = {c.id for c in bundle.concepts.values()}
    _concept_id_strs = {concept_id_to_str(cid) for cid in _concept_ids}
    _degree_by_id: dict[tuple[str, ...], int] = {cid: 0 for cid in _concept_ids}

    # Phase 3: deterministic per-node broken-link count from the EXISTING
    # ``Graph.unresolved`` model diagnostic. No new product semantics —
    # SPEC §5.3 already defines unresolved links. Grouping by source gives
    # every render path (static/live/single-file) an identical count.
    _unresolved_by_src: dict[tuple[str, ...], int] = {}
    for link in graph.unresolved:
        if link.source is None:
            continue
        _unresolved_by_src[link.source] = _unresolved_by_src.get(link.source, 0) + 1

    nodes: list[dict[str, Any]] = []
    bodies: dict[str, str] = {}
    # Phase 3 PRE-PASS: degree per node, mirroring the dedup loop's
    # invariants (drop None targets, self-loops, dangling endpoints, and
    # duplicate (src,tgt) pairs). Computing it BEFORE the node loop lets
    # the size hint below be degree-driven while keeping the server degree
    # identical to the client's unique-neighbour ``degreeOf``
    # (contract-runtime-parity).
    _seen_pairs: set[tuple[str, str]] = set()
    for link in graph.edges:
        if link.target is None:
            continue
        src = concept_id_to_str(link.source)
        tgt = concept_id_to_str(link.target)
        if src not in _concept_id_strs or tgt not in _concept_id_strs:
            continue
        key = (src, tgt)
        if key in _seen_pairs:
            continue
        _seen_pairs.add(key)
        _degree_by_id[link.source] = _degree_by_id.get(link.source, 0) + 1
        _degree_by_id[link.target] = _degree_by_id.get(link.target, 0) + 1
    # §3.2: sort concepts by id so graph node ordering (and thus the
    # emitted static __data/graph.json) is byte-stable regardless of
    # Bundle insertion order. Bundle.load happens to sort via rglob today,
    # but in-memory Bundle construction must not silently break current spec §8
    # byte-stability or graph rendering determinism.
    for c in sorted(bundle.concepts.values(), key=lambda c: c.id):
        cid_str = concept_id_to_str(c.id)
        # Pre-render the body to HTML server-side using our escaping renderer
        # (with URL scheme allowlist). This eliminates the need for a
        # client-side markdown parser (and its XSS surface). The client
        # assigns the HTML directly to innerHTML; it is safe because our
        # renderer escapes raw HTML and neutralises dangerous URL schemes.
        # Apply heading demotion for consistent document outline across
        # all render targets (concept pages AND single-file/graph views).
        bodies[cid_str] = _decorate_content_scrollports(
            normalize_document_links(_demote_headings(join_source_footnotes(markdown_to_html(c.body), c)), cid_str + '.md', 'serve')
        )
        # Sanitize resource URL through _safe_url to prevent javascript:/data:
        # XSS via the graph view (graph.js assigns data.resource to a.href).
        safe_resource = ""
        if c.resource:
            from .viewer.markdown import _safe_url as _su
            sr = _su(c.resource)
            safe_resource = sr if sr is not None else ""
        color = palette.get(c.type or "", type_color_for(c.type))
        # Phase 3: degree-driven node size. The prior body-length proxy
        # mis-sized stubs vs long pages; degree is the structural
        # importance signal the plan calls for ("size stays degree-driven
        # but with a wider, eased range"). Eased sqrt ramp saturates
        # around degree=9 so hubs clearly outweigh leaves without a few
        # super-hubs dwarfing the canvas. Orphans (degree=0) get the
        # minimum so the orphan shelf reads uniformly.
        node_degree = _degree_by_id.get(c.id, 0)
        node_size = 24 + int(28 * min(1.0, math.sqrt(node_degree) / 3.0))
        # P1-3 (iter-1): ship §7 governed keys per node so the graph detail
        # panel can render them (mirrors the concept page). Read safely and
        # default to empty lists when absent — the client hides empties.
        governed = _graph_governed_keys(c)
        grouping_meta = _graph_grouping_metadata(c)
        node_data = {
                "id": cid_str,
                "label": c.title or cid_str,
                "type": c.type or "concept",
                "description": c.description,
                "resource": safe_resource,
                "tags": list(c.tags),
                "color": color,
                # Phase 3: icon key for the node glyph (graph.js builds a
                # data-URI SVG from the shared icon path set).
                "icon": type_icon_key(c.type),
                "size": node_size,
                # Phase 3: degree + broken-link count are exposed so the
                # client hub-halo / orphan-shelf / broken-link badge all
                # read one deterministic source of truth
                # (contract-runtime-parity across static/live/single-file).
                "degree": node_degree,
                "unresolved_count": _unresolved_by_src.get(c.id, 0),
                # Recent needs the authored timestamp (ISO
                # string or ""), Attention needs the body weight to spot
                # stubs. Strings only — the client parses defensively.
                "timestamp": str(c.frontmatter.get("timestamp") or ""),
                "body_chars": len(c.body or ""),
                # §7 governed keys (P1-3 iter-1): URLs already sanitized
                # via _safe_url inside _graph_governed_keys.
                "aliases": governed["aliases"],
                "entities": governed["entities"],
                "provenance": governed["provenance"],
                "citations": governed["citations"],
                "relations": governed["relations"],
                "metadata": grouping_meta,
                "okf_metadata": c.metadata,
                "metadata_html": render_metadata(c, bundle, mode="graph"),
                "generated_at": c.generated_at,
                "graph_cluster": grouping_meta.get("graph_cluster", ""),
                "source_system": grouping_meta.get("source_system", ""),
        }
        if comment_counts is not None:
            node_data["active_comment_count"] = comment_counts.get(cid_str, 0)
        nodes.append({"data": node_data})

    # Final node ID set for the dangling-endpoint invariant (layout-graph):
    # every emitted edge source/target and every backlinks key/source must
    # belong to this exact set. Reserved index/log files are parsed as
    # ConceptIds but held in bundle.indexes/bundle.logs (NOT bundle.concepts),
    # and tolerated broken/missing links (SPEC §5.3) resolve to ConceptIds
    # that are not loaded concepts — both classes appear in graph.edges with a
    # non-None target but are absent from the node set. The Cytoscape
    # validator (graph.js ``graphCandidate``) rejects edges whose endpoints
    # are not current nodes, so omit them from the visual payload here. They
    # remain model diagnostics in ``bundle.graph().unresolved``.
    node_ids = {node["data"]["id"] for node in nodes}

    # Bundle parallel edges by (source, target), without discarding instances.
    # ``label`` remains the canonical primary relation for old consumers;
    # ``relations`` is the lossless ordered instance list for current ones.
    # Stable relation ordering contract (independent of Graph.edges input
    # order): kind, type, label, source, target, then authored identity. For
    # frontmatter relations the authored identity is the canonical JSON entry
    # (so detail/unknown extension fields participate); body links use their
    # target/form/anchor/line identity. Exact duplicates receive occurrence
    # suffixes only after this sort.
    authored_relations: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for concept in bundle.concepts.values():
        src = concept_id_to_str(concept.id)
        entries = concept.frontmatter.get("relations")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("target"), str):
                continue
            try:
                target = concept_id_to_str(concept_id_from_str(entry["target"]))
            except Exception:
                continue
            rel_type = str(entry.get("type", "relates_to"))
            canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            authored_relations.setdefault((src, target, rel_type), []).append({
                "authored_identity": canonical,
                "detail": str(entry.get("detail") or ""),
            })
    for values in authored_relations.values():
        values.sort(key=lambda value: value["authored_identity"])

    candidates: list[dict[str, Any]] = []
    for link in graph.edges:
        if link.target is None:
            continue
        src = concept_id_to_str(link.source)
        tgt = concept_id_to_str(link.target)
        # Dangling-endpoint invariant: drop edges whose source or target is
        # not in the final node set (reserved index/log files, tolerated
        # broken/missing links). Backlinks are built in this same loop, so a
        # dropped edge contributes no backlink entry either. Valid
        # current-concept edges (both endpoints in node_ids) are retained.
        if src not in node_ids or tgt not in node_ids:
            continue
        # The core origin distinguishes body links, declarations and source
        # derivations without guessing from the target string.
        is_typed_relation = link.origin in ("relations", "sources")
        # Preserve edge label so typed-relation types (references, written_by,
        # etc.) are visible in the graph view.
        has_label = link.label and not link.label.startswith("/")
        authored_identity = json.dumps(
            [link.target_raw, link.form, link.anchor or "", link.line],
            ensure_ascii=False, separators=(",", ":"),
        )
        detail = ""
        if link.origin == "sources":
            authored_identity = json.dumps([link.source_id, link.target_raw], ensure_ascii=False)
        if link.origin == "relations":
            authored = authored_relations.get((src, tgt, str(link.label)), [])
            if authored:
                metadata = authored.pop(0)
                authored_identity = metadata["authored_identity"]
                detail = metadata["detail"]
        relation: dict[str, Any] = {
            "id": "",
            "label": link.label or "related",
            "type": link.label if is_typed_relation and has_label else "link",
            "kind": "relation" if is_typed_relation else "body_link",
            "direction": "outgoing",
            "source": src,
            "target": tgt,
            "target_raw": link.target_raw,
            "authored_identity": authored_identity,
            "origin": link.origin,
            "source_id": link.source_id,
        }
        if detail:
            relation["detail"] = detail
        candidates.append(relation)

    stable_relation_key = lambda item: (
        item["kind"], item["type"], item["label"], item["source"],
        item["target"], item["authored_identity"],
    )
    candidates.sort(key=stable_relation_key)
    edges: list[dict[str, Any]] = []
    edge_index: dict[tuple[str, str], int] = {}
    backlinks: dict[str, list[str]] = {}
    occurrences: dict[tuple[str, ...], int] = {}
    def stable_edge_id(source: str, target: str) -> str:
        canonical = json.dumps([source, target], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return f"edge-{len(canonical):x}-{hashlib.sha256(canonical).hexdigest()}"

    for relation in candidates:
        src, tgt = relation["source"], relation["target"]
        key = (src, tgt)
        identity_key = tuple(str(value) for value in stable_relation_key(relation))
        occurrence = occurrences.get(identity_key, 0)
        occurrences[identity_key] = occurrence + 1
        identity = json.dumps([*identity_key, occurrence], ensure_ascii=False, separators=(",", ":"))
        relation["id"] = "rel-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        if key not in edge_index:
            edge_index[key] = len(edges)
            edges.append({"data": {"id": stable_edge_id(src, tgt), "source": src, "target": tgt, "relations": []}})
            backlinks.setdefault(tgt, []).append(src)
        edges[edge_index[key]]["data"]["relations"].append(relation)

    for edge in edges:
        data = edge["data"]
        relations = data["relations"]
        primary = next((item for item in relations if item["kind"] == "relation"), relations[0])
        data["multiplicity"] = len(relations)
        data["primary_relation"] = primary
        if primary["kind"] == "relation":
            data["label"] = primary["type"]
        elif primary["label"] and not primary["label"].startswith("/"):
            data["label"] = primary["label"]

    external = [
        {
            "source": concept_id_to_str(e.source),
            "target_raw": e.target_raw,
            "label": e.label,
        }
        for e in graph.external
    ]

    # iter2 P2-1: explicit sort for byte-stable output (current spec §3/§8). Iter-1
    # sorted nodes but missed these sibling lists; the dedup loop above
    # preserves graph.edges insertion order which is nondeterministic for
    # in-memory Bundle construction. Sort all output lists by stable keys.
    edges.sort(key=lambda e: (e["data"]["source"], e["data"]["target"]))
    external.sort(key=lambda e: (e["source"], e["target_raw"]))
    backlinks = {k: sorted(v) for k, v in sorted(backlinks.items())}

    return {
        "name": name or bundle.name,
        "bundle_revision": bundle_revision(bundle),
        "nodes": nodes,
        "edges": edges,
        "raw_relationship_count": sum(edge["data"]["multiplicity"] for edge in edges),
        "external": external,
        "bodies": bodies,
        # Navigation documents are not graph concepts or synthetic nodes.
        "documents": {
            rel.as_posix(): {"document": rel.as_posix(),
                            "html": normalize_document_links(markdown_to_html(getattr(doc, 'body', doc.raw_text)), rel.as_posix(), 'serve')}
            for rel, doc in sorted({**bundle.indexes, **bundle.logs}.items())
            if doc.path.resolve().is_relative_to(bundle.root.resolve())
        },
        "types": types,
        "palette": palette,
        "backlinks": backlinks,
        # Phase 3: icon-key → SVG inner markup, so graph.js can build node
        # glyph data-URIs from the same curated set the wiki uses.
        "icon_paths": type_icon_paths(),
    }


# ---------------------------------------------------------------------------
# Theme button (P2-74) + shared topbar nav fragment (P2-61)
# ---------------------------------------------------------------------------

# Resolved data-theme values (family + mode). viewer/static/theme.js is the
# SOLE owner of theme state; these concrete values are the only ones painted
# on <html> and the only ones with a wiki.css token block. KEEP IN SYNC with
# theme.js (THEMES/LEGACY_THEMES), config.py (_ALLOWED_STUDIO_THEMES) and
# viewer/assets.py (_ALLOWED_THEMES) — tests/test_theme_contract.py enforces
# the parity. Technical and Swiss retain their established shared palette;
# Purple and Coffee add optional editorial palettes. Every family has a
# meaningful geometry/surface treatment in addition to colour (see wiki.css).
_THEMES: tuple[str, ...] = (
    "technical-light", "technical-dark", "swiss-light", "swiss-dark",
    "purple-light", "purple-dark", "coffee-light", "coffee-dark",
)
# Legacy localStorage["okf-theme"] concrete values → new resolved theme.
# Mirrors theme.js LEGACY_THEMES; "light" (the V2 default look) maps to
# Technical-light so existing users keep exactly the visuals they had.
_LEGACY_THEME_MIGRATIONS: dict[str, str] = {
    "light": "technical-light", "dark": "technical-dark",
    "pastel": "swiss-light", "sepia": "swiss-light", "midnight": "technical-dark",
}
# Server-side default when no concrete theme is configured. This is a concrete
# paint hint only; the configured preference is _DEFAULT_CONFIGURED_THEME and
# theme.js resolves Technical Auto from the OS before the body can paint.
_DEFAULT_THEME = "technical-light"
_DEFAULT_CONFIGURED_THEME = "auto"
_CONTRASTS: tuple[str, ...] = ("normal", "soft")
_BORDERS: tuple[str, ...] = ("on", "off")
_DENSITIES: tuple[str, ...] = ("comfortable", "compact")
# Fresh-visitor defaults (Technical, Auto/System mode, Normal contrast,
# Borders Off, Compact density). KEEP IN SYNC with theme.js DEFAULT_* and
# viewer/assets.py _DEFAULT_CONFIG. Density is a user-only preference (no
# configured value), so the server always paints the default.
_DEFAULT_CONTRAST = "normal"
_DEFAULT_BORDER = "off"
_DEFAULT_DENSITY = "compact"


def _theme_preference(config: dict[str, Any] | None) -> tuple[str, str, str]:
    """Return the validated configured theme preference and modifiers.

    ``data-theme`` is deliberately not used here. It is a concrete first
    paint hint and must never turn an absent configured Auto preference into an
    explicit Light/Dark choice. ``viewer.assets.load_config`` already filters
    user config, but this second boundary keeps render callers fail-closed when
    supplied a hand-built config dictionary in tests or an embedding.
    """
    cfg = config or {}
    theme_value = cfg.get("theme")
    theme = (
        theme_value if theme_value == "auto" or theme_value in _THEMES
        else _DEFAULT_CONFIGURED_THEME
    )
    contrast_value = cfg.get("contrast")
    contrast = contrast_value if contrast_value in _CONTRASTS else _DEFAULT_CONTRAST
    border_value = cfg.get("border")
    border = border_value if border_value in _BORDERS else _DEFAULT_BORDER
    return theme, contrast, border


def _resolve_initial_theme(value: str | None) -> str:
    """Normalise a configured preference to a concrete first-paint theme.

    Accepts the new family-mode vocabulary directly. The retired legacy
    concrete names (light/dark/pastel/sepia/midnight) are migrated to their
    new resolved theme so an older viewer config.json keeps working. ``"auto"``
    and invalid/absent values use the Technical-light paint hint. The caller
    emits the original validated preference separately; theme.js owns the
    follow-OS resolution and never infers preference from this paint hint.
    """
    if value in _THEMES:
        return value
    if value and value in _LEGACY_THEME_MIGRATIONS:
        return _LEGACY_THEME_MIGRATIONS[value]
    return _DEFAULT_THEME


def _theme_modifier_attrs(contrast: str = _DEFAULT_CONTRAST,
                          border: str = _DEFAULT_BORDER) -> str:
    """Root ``<html>`` modifier attributes for the configured preference.

    The server paints the configured contrast/border and the default density
    so no-JS readers get the same defaults as a fresh scripted visitor and
    the first paint matches what theme.js resolves when no saved user
    preference exists. theme.js (parser-blocking in ``<head>``) still owns
    the final values and overwrites them before the body paints.
    """
    contrast = contrast if contrast in _CONTRASTS else _DEFAULT_CONTRAST
    border = border if border in _BORDERS else _DEFAULT_BORDER
    return (
        f' data-okf-contrast="{contrast}" data-okf-border="{border}"'
        f' data-okf-density="{_DEFAULT_DENSITY}"'
    )


def _theme_config_html(theme: str, contrast: str = _DEFAULT_CONTRAST,
                       border: str = _DEFAULT_BORDER) -> str:
    """Emit the non-executable configured preference consumed by theme.js.

    This block is present on live, SPA, static, and single-file targets. It is
    intentionally separate from the concrete ``data-theme`` attribute so a
    Technical Auto configuration remains Auto when the OS is dark.
    """
    configured_theme = (
        theme if theme == "auto" or theme in _THEMES else _DEFAULT_CONFIGURED_THEME
    )
    configured_contrast = contrast if contrast in _CONTRASTS else _DEFAULT_CONTRAST
    configured_border = border if border in _BORDERS else _DEFAULT_BORDER
    payload = json.dumps(
        {"theme": configured_theme, "contrast": configured_contrast, "border": configured_border},
        separators=(",", ":"),
    )
    return f'<script type="application/json" id="okf-theme-config">{payload}</script>'


def _appearance_control_html(initial_theme: str,
                             initial_mode: str = "auto",
                             initial_contrast: str = _DEFAULT_CONTRAST,
                             initial_border: str = _DEFAULT_BORDER) -> str:
    """Server-side Appearance control (no FOUC, keyboard-accessible).

    Emits a native trigger plus a form with real labelled radio inputs for
    Family, Mode, Contrast, Border, and Density. ``viewer/static/theme.js`` (loaded
    before first paint) is the sole owner of preference state and wires
    open/close, Arrow/Home/End/Escape/Tab behaviour. The server only seeds
    the pre-JavaScript checked state from the configured preference.

    The trigger keeps the historical ``#okf-theme`` id so the existing
    no-JS CSS (``body:not([data-okf-js]) #okf-theme { display:none }``) hides
    it when JavaScript is unavailable; the menu starts ``hidden`` and is
    therefore inert without theme.js.
    """
    theme = initial_theme if initial_theme in _THEMES else _DEFAULT_THEME
    initial_contrast = (
        initial_contrast if initial_contrast in _CONTRASTS else _DEFAULT_CONTRAST
    )
    initial_border = initial_border if initial_border in _BORDERS else _DEFAULT_BORDER
    initial_mode = initial_mode if initial_mode in ("auto", "light", "dark") else "auto"
    family = theme.rsplit("-", 1)[0]

    def _opt(group: str, value: str, label: str, sub: str, checked: str) -> str:
        is_checked = checked == "true"
        input_id = f"okf-appearance-{group}-{value}"
        preview = (
            f'<span class="okf-appearance__preview okf-appearance__preview--{value}" '
            f'aria-hidden="true"><span></span><span></span><span></span></span>'
            if group == "family" else ""
        )
        return (
            f'<label for="{input_id}" class="okf-appearance__opt" aria-checked="{checked}">'
            f'<input id="{input_id}" type="radio" name="okf-theme-{group}"'
            f' value="{value}" data-okf-set="{group}" data-okf-val="{value}"'
            f' aria-checked="{checked}" tabindex="{"0" if is_checked else "-1"}"'
            f'{" checked" if is_checked else ""}>'
            '<span class="okf-appearance__check" aria-hidden="true">\u2713</span>'
            f'{preview}'
            f'<span class="okf-appearance__label">{label}</span>'
            f'<span class="okf-appearance__sub">{sub}</span>'
            '</label>'
        )

    return (
        '<button id="okf-theme" type="button" class="okf-btn okf-appearance-trigger" '
        'aria-haspopup="dialog" aria-expanded="false" '
        'aria-controls="okf-appearance-menu" '
        'title="Appearance \u2014 theme, colour mode, contrast, borders, and density">'
        f'<span class="okf-appearance-trigger__swatch okf-appearance-trigger__swatch--{_esc_attr_qs(family)}" aria-hidden="true"></span>'
        '<span class="okf-appearance-trigger__label">'
        '<span class="okf-appearance-trigger__prefix">Appearance \u00b7 </span>'
        f'<span class="okf-appearance-trigger__family">{family.title()}</span>'
        '</span></button>'
        '<form id="okf-appearance-menu" role="dialog" aria-label="Appearance" hidden>'
        '<h2 class="sr-only">Appearance</h2>'
        '<fieldset class="okf-appearance__group" role="group" aria-labelledby="okf-appearance-mode-label">'
        '<legend id="okf-appearance-mode-label" class="okf-appearance__legend">Colour mode</legend>'
        + _opt("mode", "auto", "Auto", f"{family.title()} family + follow OS colour mode", "true" if initial_mode == "auto" else "false")
        + _opt("mode", "light", "Light", "", "true" if initial_mode == "light" else "false")
        + _opt("mode", "dark", "Dark", "", "true" if initial_mode == "dark" else "false")
        + '</fieldset>'
        '<fieldset class="okf-appearance__group" role="group" aria-labelledby="okf-appearance-density-label">'
        '<legend id="okf-appearance-density-label" class="okf-appearance__legend">Density</legend>'
        + _opt("density", "comfortable", "Comfortable", "more room in interface lists and controls", "true" if _DEFAULT_DENSITY == "comfortable" else "false")
        + _opt("density", "compact", "Compact", "less interface space; reading measure unchanged", "true" if _DEFAULT_DENSITY == "compact" else "false")
        + '</fieldset>'
        '<details class="okf-appearance__advanced"><summary>Advanced appearance</summary>'
        '<fieldset class="okf-appearance__group" role="group" aria-labelledby="okf-appearance-family-label">'
        '<legend id="okf-appearance-family-label" class="okf-appearance__legend">Family</legend>'
        + _opt("family", "technical", "Technical", "application default", "true" if family == "technical" else "false")
        + _opt("family", "swiss", "Swiss", "hairline grid", "true" if family == "swiss" else "false")
        + _opt("family", "purple", "Purple", "editorial violet", "true" if family == "purple" else "false")
        + _opt("family", "coffee", "Coffee", "paper and espresso", "true" if family == "coffee" else "false")
        + '</fieldset>'
        '<fieldset class="okf-appearance__group" role="group" aria-labelledby="okf-appearance-contrast-label">'
        '<legend id="okf-appearance-contrast-label" class="okf-appearance__legend">Contrast</legend>'
        + _opt("contrast", "normal", "Normal", "standard surfaces", "true" if initial_contrast == "normal" else "false")
        + _opt("contrast", "soft", "Soft", "quieter surfaces", "true" if initial_contrast == "soft" else "false")
        + '</fieldset>'
        '<fieldset class="okf-appearance__group" role="group" aria-labelledby="okf-appearance-border-label">'
        '<legend id="okf-appearance-border-label" class="okf-appearance__legend">Borders</legend>'
        + _opt("border", "on", "On", "visible frames", "true" if initial_border == "on" else "false")
        + _opt("border", "off", "Off", "focus and status cues remain", "true" if initial_border == "off" else "false")
        + '</fieldset>'
        '</details>'
        '<p class="okf-appearance__note">Density changes interface spacing, never reading width or minimum control targets.</p>'
        '<button type="button" class="okf-appearance__reset" data-okf-action="reset">Reset to Technical Auto, compact, borders off</button>'
        '</form>'
    )


def _nav_controls_html(
    *,
    root_prefix: str,
    graph_link: str,
    initial_theme: str,
    initial_mode: str = "auto",
    initial_contrast: str = _DEFAULT_CONTRAST,
    initial_border: str = _DEFAULT_BORDER,
    current_query: str = "",
    mode: str = "serve",
) -> str:
    """Shared topbar controls fragment (P2-61).

    Renders the search form + Graph + Index + theme button. Used by the
    concept, index, and search templates so the chrome is identical
    across all three. (Graph view and single-file viewers keep their own
    topbars because they carry typeahead/filter/layout controls.)

    P1-3: in ``static`` mode the search form posts to ``__search.html``
    (the page is emitted at that path; the live server never runs to
    rewrite the extensionless URL). ``spa``/``serve`` keep the bare
    ``__search`` path because the live router handles it.
    """
    search_value = (
        f' value="{_esc_attr_qs(current_query)}"' if current_query else ""
    )
    index_href = root_prefix or "./"
    search_target = f"{root_prefix}__search{'.html' if mode == 'static' else ''}"
    return (
        '<div class="okf-topbar__controls">'
        f'<form action="{search_target}" method="get" role="search" class="okf-search-form">'
        '<input type="search" name="q" placeholder="Search\u2026" autocomplete="off"'
        f' aria-label="Search"{search_value}>'
        '</form>'
        f'<a class="okf-btn" href="{graph_link}">Graph</a>'
        f'<a class="okf-btn" href="{index_href}">Index</a>'
          f'{_appearance_control_html(initial_theme, initial_mode, initial_contrast, initial_border)}'
        '</div>'
    )


def _application_shell_html(
    *,
    name: str,
    bundle_href: str,
    search_href: str,
    index_href: str,
    graph_href: str,
    current: str | None,
    mode: str,
    initial_theme: str,
    initial_mode: str,
    initial_contrast: str,
    initial_border: str,
    search_query: str = "",
) -> str:
    """Bridge renderer-owned route state into the canonical shell model."""
    if current not in {None, "search", "index", "graph"}:
        raise ValueError(f"unsupported shell destination: {current}")
    return render_application_shell(
        ApplicationShell(
            bundle_name=name,
            bundle_href=bundle_href,
            search_href=search_href,
            index_href=index_href,
            graph_href=graph_href,
            current=current,  # type: ignore[arg-type]
            studio_available=mode == "serve",
            search_query=search_query,
        ),
        _appearance_control_html(
            initial_theme, initial_mode, initial_contrast, initial_border,
        ),
    )


def _render_not_found_page(*, name: str, path: str, config: dict[str, Any]) -> str:
    """Minimal live document recovery using the canonical application shell.

    No concept lookup, suggestions, or query-string reflection belongs here.
    The server retains status, security headers, and Studio authority ownership.
    """
    theme, contrast, border = _theme_preference(config)
    initial_theme = _resolve_initial_theme(theme)
    shell = _application_shell_html(
        name=name, bundle_href="/", search_href="/__search", index_href="/",
        graph_href="/__graph", current=None, mode="serve",
        initial_theme=initial_theme,
        initial_mode="auto" if theme == "auto" else theme.rsplit("-", 1)[1],
        initial_contrast=contrast, initial_border=border,
    )
    return (
        '<!doctype html><html lang="en"'
        f' data-theme="{_esc_attr_qs(initial_theme)}"{_theme_modifier_attrs(contrast, border)}><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Page not found — {_esc(name)}</title>'
        '<link rel="stylesheet" href="/__static/wiki.css">'
        '<link rel="stylesheet" href="/__static/shell.css">'
        '</head><body data-okf-mode="serve">'
        '<a class="okf-skip-link" href="#okf-main">Skip to content</a>'
        + shell
        + '<main id="okf-main" class="okf-index" tabindex="-1">'
        '<h1>Page not found</h1>'
        f'<p>The requested page <code>/{_esc(path)}</code> is not available.</p>'
        '<p><a class="okf-btn" href="/">Home · Browse the index</a> '
        '<a class="okf-btn" href="/__search">Search this bundle</a></p>'
        '</main><script src="/__static/wiki.js" defer></script></body></html>'
    )


#: Server-rendered copy for an import page that cannot import at all. Wording
#: for ``local_only`` is the X2 design copy; the page never offers controls
#: that the POST routes would refuse anyway.
_FEEDBACK_IMPORT_BLOCKED = {
    "local_only": "Import is available only on the local Studio, not through a tunnel.",
    "read_only": "This Studio is read-only, so notes can't be imported here. "
                 "Start the Studio without --no-edit to import.",
    "unavailable": "Import needs the local Studio, which isn't enabled on this server.",
}
FEEDBACK_IMPORT_RETENTION = (
    "Retries return the original result for up to 24 hours (7 days as a compact record). "
    "After that, the same note is refused rather than imported twice."
)


def render_feedback_import_page(*, name: str, config: dict[str, Any], availability: str,
                                max_note_bytes: int, preview_ttl_seconds: int) -> str:
    """Local operator page for importing one portable feedback note (X2).

    Live-server only (never emitted by a build). ``availability`` is
    presentation: ``available`` renders the file chooser that
    ``feedback-import.js`` drives; anything else renders the refusal instead.
    The POST routes stay the authority. Built-in markup only, never a bundle
    template override, because the page carries the local Studio token.
    """
    if availability not in {"available", *_FEEDBACK_IMPORT_BLOCKED}:
        raise ValueError(f"unsupported feedback import availability: {availability}")
    theme, contrast, border = _theme_preference(config)
    initial_theme = _resolve_initial_theme(theme)
    shell = _application_shell_html(
        name=name, bundle_href="/", search_href="/__search", index_href="/",
        graph_href="/__graph", current=None, mode="serve",
        initial_theme=initial_theme,
        initial_mode="auto" if theme == "auto" else theme.rsplit("-", 1)[1],
        initial_contrast=contrast, initial_border=border,
    )
    icon = re.search(r'<link rel="icon"[^>]*>', load_template("index_page.html"))
    page_config = json.dumps({
        "version": 1, "availability": availability,
        "max_note_bytes": int(max_note_bytes),
        "preview_ttl_seconds": int(preview_ttl_seconds),
    }, separators=(",", ":")).replace("</", "<\\/")
    limit_kib = max(1, int(max_note_bytes) // 1024)
    if availability == "available":
        work = (
            '<p class="okf-fbi__notice okf-fbi__notice--info okf-nojs-fallback">'
            'Checking and importing a note needs JavaScript in this browser.</p>\n'
            '<section class="okf-fbi__panel okf-js-only" aria-labelledby="okf-fbi-choose-title">\n'
            '<h2 id="okf-fbi-choose-title" class="okf-fbi__panel-title">Choose a note file</h2>\n'
            '<div class="okf-fbi__field">\n'
            '<label for="okf-fbi-file">Feedback note file</label>\n'
            '<input id="okf-fbi-file" class="okf-fbi__file" type="file" '
            'accept=".json,application/json" aria-describedby="okf-fbi-file-hint">\n'
            f'<p id="okf-fbi-file-hint" class="okf-fbi__hint">One <code>.okf-feedback.json</code> '
            f'file, up to {limit_kib} KiB. Choosing a file only checks it; nothing is imported '
            'until you confirm.</p>\n'
            '</div>\n</section>\n'
            '<section id="okf-fbi-result" class="okf-fbi__panel okf-fbi__result" '
            'aria-labelledby="okf-fbi-result-title" hidden></section>\n'
        )
    else:
        work = (
            '<section class="okf-fbi__panel" aria-labelledby="okf-fbi-blocked-title">\n'
            '<h2 id="okf-fbi-blocked-title" class="okf-fbi__panel-title">Import unavailable</h2>\n'
            # The flag is one unbreakable token: a wrap inside it garbles the command.
            '<p class="okf-fbi__notice okf-fbi__notice--error">'
            + _esc(_FEEDBACK_IMPORT_BLOCKED[availability]).replace(
                "--no-edit", '<code class="okf-fbi__flag">--no-edit</code>')
            + '</p>\n'
            '</section>\n'
        )
    return (
        f'<!DOCTYPE html>\n<html lang="en" data-theme="{_esc_attr_qs(initial_theme)}"'
        f'{_theme_modifier_attrs(contrast, border)}>\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_RESERVED_CSP}">\n'
        f'<title>Import a feedback note — {_esc(name)}</title>\n'
        + (icon.group(0) + '\n' if icon else '')
        + '<link rel="stylesheet" href="/__static/wiki.css">\n'
        '<link rel="stylesheet" href="/__static/shell.css">\n'
        '<link rel="stylesheet" href="/__static/feedback-import.css">\n'
        + _theme_config_html(theme, contrast, border) + '\n'
        f'<script type="application/json" id="okf-feedback-import-config">{page_config}</script>\n'
        '</head>\n'
        '<body class="okf-viewer okf-viewer--feedback-import" data-okf-mode="serve" '
        'data-okf-enhance="1" data-okf-studio="1">\n'
        '<a href="#okf-main" class="okf-skip-link">Skip to content</a>\n'
        + shell + '\n'
        f'<main id="okf-main" class="okf-fbi" tabindex="-1" data-okf-feedback-import '
        f'data-availability="{_esc_attr_qs(availability)}" data-state="idle">\n'
        '<div class="okf-fbi__lane">\n'
        '<header class="okf-fbi__header">\n'
        '<p class="okf-fbi__kind"><span class="okf-reserved__kind">Local Studio</span></p>\n'
        '<h1 class="okf-fbi__title">Import a feedback note</h1>\n'
        '<p class="okf-fbi__lede">A reader of an exported copy of this bundle can write a note '
        'about one page and send you the file. Check the note here before it becomes a comment.</p>\n'
        '</header>\n'
        + work +
        '<section class="okf-fbi__help" aria-labelledby="okf-fbi-help-title">\n'
        '<h2 id="okf-fbi-help-title" class="okf-fbi__help-title">What importing does</h2>\n'
        '<ul>\n'
        '<li>An imported note becomes an ordinary open comment on its page, recorded as you. '
        'Readers and agents of this bundle see it like any other comment. '
        'It is a page comment: any quote is shown with it but is not highlighted or pinned to exact text.</li>\n'
        '<li>A note imports only while its page is byte-for-byte the version it was written about.</li>\n'
        '<li>The file can&#x27;t prove who wrote it. Any name or time in it is shown as an unverified claim.</li>\n'
        f'<li>{_esc(FEEDBACK_IMPORT_RETENTION)}</li>\n'
        '</ul>\n</section>\n'
        '<div id="okf-fbi-live" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>\n'
        '</div>\n</main>\n'
        '<script src="/__static/wiki.js" defer></script>\n'
        '<script src="/__static/feedback-import.js" defer></script>\n'
        '</body>\n</html>\n'
    )


# ---------------------------------------------------------------------------
# Single-file viewer
# ---------------------------------------------------------------------------

_CDN_SCRIPTS = (
    # Cytoscape.js + the fCoSE layout chain are loaded from CDN. Markdown
    # rendering happens server-side (see ``build_graph_data``) so marked.js
    # is NOT needed, which removes a client-side XSS surface and a
    # supply-chain dep.
    # Every tag is pinned to an exact version AND carries an SRI hash
    # (computed via:
    #   curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A
    # ). The browser will REFUSE to execute a script if the CDN response
    # does not match its hash, mitigating CDN-compromise / MITM risk.
    #
    # fCoSE (phase 3) is the quality force layout the graph uses by
    # default; its UMD chain is layout-base → cose-base → cytoscape-fcose
    # (each reads the previous from a browser global) and cytoscape-fcose
    # self-registers against window.cytoscape. graph.js falls back to
    # plain cose when the chain fails to load (offline / cdn:false).
    '<script src="https://cdn.jsdelivr.net/npm/cytoscape@3.34.3/dist/cytoscape.min.js"'
    ' integrity="sha384-qPKQxl9uMXOw7vSTUDAnpUilhLuulovw6P5Z4db4bqxW5VhumS7przEmHX0iM0Oc"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/layout-base@2.0.1/layout-base.js"'
    ' integrity="sha384-5E2lB9AIGE6LRCnOOSTnZRlYZFZ01iMeN2fw97Z1r4Z/kXALxKw2AC+ZzQqoeDsG"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/cose-base@2.2.0/cose-base.js"'
    ' integrity="sha384-RswRBkrMsPUYpJLbZ1CVA08zbNzAkykE2oGJTujBwfjWNdfxv2WVjLJNqv1LhAOp"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/cytoscape-fcose@2.2.0/cytoscape-fcose.js"'
    ' integrity="sha384-uk5Wbjq1+KqUdHO30w7N7GrEGdzBhaJeW9o/ANF6v9+yx3M/cBmoX51C000JNCUf"'
    ' crossorigin="anonymous"></script>\n'
    # dagre powers the "Flow" lens (layered/Sugiyama layout for the
    # directed dependency subgraph — the evidence-backed layout for
    # direction/flow reading). cytoscape-dagre self-registers; graph.js
    # falls back to directed breadthfirst when the chain is absent.
    '<script src="https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js"'
    ' integrity="sha384-2IH3T69EIKYC4c+RXZifZRvaH5SRUdacJW7j6HtE5rQbvLhKKdawxq6vpIzJ7j9M"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"'
    ' integrity="sha384-u69h9ebXeSjlg6q/rb1zKTRAGu/h8deCl0409xpS/QJctMKnc4M9Fzkm01VOQdeF"'
    ' crossorigin="anonymous"></script>'
)


def _json_for_script(data: Any) -> str:
    """JSON-encode ``data`` for safe embedding inside ``<script>...</script>``.

    Escapes ``<``, ``>``, ``&`` to their ``\\u003c`` / ``\\u003e`` /
    ``\\u0026`` JSON escapes, plus the JS-only line separators
    ``\\u2028`` / ``\\u2029``. This prevents:

        1. HTML parser breakout via ``</script>`` sequences in the data
           (the HTML spec closes the script element on the first
           ``</script`` sequence, regardless of JS string context).
        2. ``<!--`` sequences from compatibility parser quirks.
        3. JS syntax errors from raw line separators (valid in JSON
           strings, invalid in JS string literals pre-ES2019).

    The browser's JS engine unescapes the sequences back to the original
    characters AT EXECUTION TIME, after the HTML parser has already
    finished, so the data round-trips losslessly.
    """
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _read_bundle_comment_summary(bundle: Bundle) -> CommentSummaryV1:
    """Read one configured, locked summary snapshot without starting Studio."""
    from .config import OkfConfig

    from .studio import _configured_session_root
    session = _configured_session_root(bundle.root, OkfConfig.load(bundle.root).studio.session_path)
    authority = session if hasattr(session, "display_path") else None
    session_path = authority.path if authority is not None else session
    concepts = {concept_id_to_str(concept.id) for concept in bundle.concepts.values()}
    try:
        return read_comment_summary(session_path, graph_concepts=concepts)
    finally:
        if authority is not None:
            authority.close()


def _read_bundle_engagement_events(bundle: Bundle) -> list[dict[str, Any]] | None:
    """Read a bounded, authoritative build-time activity snapshot."""
    from .config import OkfConfig
    from .studio import _configured_session_root
    session = _configured_session_root(bundle.root, OkfConfig.load(bundle.root).studio.session_path)
    authority = session if hasattr(session, "display_path") else None
    session_path = authority.path if authority is not None else session
    try:
        return read_event_snapshot(session_path, limit=500)
    except (CommentSummaryUnavailable, OSError, ValueError):
        # Recent activity is optional portable decoration. Unavailable,
        # malformed, torn, or unsafe authority means "unknown": omit the rail
        # without mutating/repairing session state or failing the build.
        return None
    finally:
        if authority is not None:
            authority.close()


def _recent_engagement_rows(
    bundle: Bundle, events: list[dict[str, Any]] | None, *, limit: int = 5,
) -> list[dict[str, str]]:
    """Normalize event concept shapes into current, stable recent rows."""
    if events is None:
        return []
    known = {concept_id_to_str(c.id): c for c in bundle.concepts.values()}
    latest: dict[str, dict[str, str]] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in {
            "changed", "created", "removed", "activity",
        } or not isinstance(event.get("ts"), str):
            continue
        ids = list(event.get("ids")) if isinstance(event.get("ids"), list) else []
        for key in ("concept", "concept_id"):
            if isinstance(event.get(key), str):
                ids.append(event[key])
        for concept_id in ids:
            if not isinstance(concept_id, str) or concept_id not in known:
                continue
            current = latest.get(concept_id)
            if current is None or current["ts"] < event["ts"]:
                latest[concept_id] = {
                    "id": concept_id,
                    "title": known[concept_id].title,
                    "ts": event["ts"],
                    "type": str(event.get("type")),
                    "action": str(event.get("action") or event.get("type")),
                    "actor": str(event.get("actor") or event.get("origin") or "system"),
                    "event_id": str(
                        event.get("event_id") or event.get("event_cursor")
                        or (f'{event.get("id")}|{event.get("seq")}'
                            if event.get("id") is not None else "")
                    ),
                }
    rows = [row for row in latest.values()
            if row["type"] != "removed" and row["event_id"]]
    rows.sort(key=lambda row: (row["ts"], row["id"]), reverse=True)
    return rows[:limit]


def _recent_concepts_by_instant(bundle: Bundle, *, limit: int = 5) -> list[Concept]:
    """Newest-first concept heads by the OKF 0.2 effective change instant.

    F7: orders through the metadata owner (``effective_instant_key``), never
    the raw legacy ``timestamp`` key — ``generated.at`` with a nonempty actor
    wins, the legacy key counts only when ``generated`` is absent, and a
    present-but-invalid mapping or empty ``by`` stays unknown-time. Dated
    concepts come first (mixed authored offsets compared as real instants),
    unknown-time concepts follow, both in stable concept-id order. No date is
    ever invented. This is a frontmatter recency head, distinct from the
    event-history rows above.
    """
    keys = {c.id: effective_instant_key(c.frontmatter) for c in bundle.concepts.values()}
    ordered = sorted(bundle.concepts.values(), key=lambda c: concept_id_to_str(c.id))
    dated = sorted((c for c in ordered if keys[c.id]),
                   key=lambda c: keys[c.id], reverse=True)
    undated = [c for c in ordered if not keys[c.id]]
    return (dated + undated)[:limit]


# F14: reader words for the machine event verbs on the human-facing recency
# rail. The raw ``action``/``actor`` audit values stay on the row element as
# data attributes; unknown actions keep their raw record (nothing invented).
_RECENT_READER_VERBS = {
    "changed": "edited",
    "updated": "edited",
    "created": "created",
    "removed": "removed",
}
# Transports, not authors: presenting "disk changed" as a person/action pair
# misreads the event record. These origins become channel phrases instead.
_RECENT_CHANNEL_ACTORS = frozenset({"disk", "mutator", "undo"})


def _recent_row_reader_text(row: dict[str, str]) -> str:
    """Human presentation of one recent row's actor/action audit pair.

    Machine verbs map to reader words ("disk changed" → "edited on disk");
    real actors keep their audit identity ("agent:x edited"). Unknown values
    fall back to the raw record verbatim.
    """
    actor, action = row["actor"], row["action"]
    verb = _RECENT_READER_VERBS.get(action.strip().lower())
    if actor in _RECENT_CHANNEL_ACTORS:
        channel = " on disk" if actor == "disk" else f" via {actor}"
        return (verb or action) + channel
    return f"{actor} {verb or action}".strip()


def _recent_time_text(ts: str) -> str:
    """Deterministic human time text for a recent row (build-time safe).

    Relative phrasing would go stale in static builds, so the visible text is
    the event's real instant in UTC; the authored ``ts`` stays on the
    ``datetime`` attribute. Unparseable values show the raw record.
    """
    instant = parse_instant(ts)
    if instant is None:
        return ts
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def graph_export_svg(graph_data: dict[str, Any], *, title_id: str = "okf-graph-export-title", limit: int = 24) -> str:
    """Return the dependency-free canonical graph SVG used by every static graph surface.

    The payload, ordering, positions, type shapes, icons, edge dashes and count
    chips are deliberately resolved here, rather than independently by each
    consumer.  The result is byte-stable and safe to place in HTML.
    """
    nodes = sorted(graph_data.get("nodes", []), key=lambda n: str(n.get("data", {}).get("id", "")))[:limit]
    if not nodes:
        return ""
    count = len(nodes)
    positions: dict[str, tuple[float, float]] = {}
    columns = min(6, max(1, math.ceil(math.sqrt(count * 1.5))))
    rows = math.ceil(count / columns)
    x_step = 500 / max(1, columns - 1) if columns > 1 else 0
    y_step = 186 / max(1, rows - 1) if rows > 1 else 0
    for index, node in enumerate(nodes):
        row, column = divmod(index, columns)
        positions[str(node["data"]["id"])] = (30 + column * x_step, 28 + row * y_step)
    node_specs: list[dict[str, Any]] = []
    occupied: list[tuple[float, float, float, float]] = []
    for node in nodes:
        data=node["data"];x,y=positions[str(data["id"])];radius=9+min(7,int(data.get("degree",0)));label=str(data.get("label") or data["id"]);short=label if len(label)<=10 else label[:9]+"…";width=min(66,max(24,7*len(short)+8));ly=min(243,y+radius+14);lx=max(2,min(558-width,x-width/2));node_specs.append({"data":data,"x":x,"y":y,"radius":radius,"short":short,"label_width":width,"label_x":lx,"label_y":ly});occupied.extend([(x-radius-2,y-radius-2,x+radius+2,y+radius+2),(lx-2,ly-13,lx+width+2,ly+5)])
    def chip_clear(cx: float, cy: float, radius: float = 11) -> bool:
        if cx-radius<0 or cy-radius<0 or cx+radius>560 or cy+radius>250:return False
        return all(cx+radius<=x1 or cx-radius>=x2 or cy+radius<=y1 or cy-radius>=y2 for x1,y1,x2,y2 in occupied)
    edge_svg = []
    suppressed_chips: list[str] = []
    for edge in sorted(graph_data.get("edges", []), key=lambda e: str(e.get("data", {}).get("id", ""))):
        data = edge.get("data", {}); source = positions.get(str(data.get("source"))); target = positions.get(str(data.get("target")))
        if source and target:
            relations = data.get("relations") if isinstance(data.get("relations"), list) else []
            label = str((relations[0].get("type") if relations and isinstance(relations[0], dict) else data.get("label")) or "")
            dash = (sum(label.encode("utf-8")) % 3) + 1 if label else 0
            dash_attr = (' stroke-dasharray="6 3"' if dash == 1 else ' stroke-dasharray="2 3"' if dash == 2 else ' stroke-dasharray="10 3 2 3"' if dash else '')
            edge_svg.append(f'<line data-relation="{_esc_attr_qs(label)}" x1="{source[0]:.1f}" y1="{source[1]:.1f}" x2="{target[0]:.1f}" y2="{target[1]:.1f}"{dash_attr} marker-end="url(#okf-export-arrow)"/>')
            if len(relations) > 1:
                dx,dy=target[0]-source[0],target[1]-source[1];length=max(1.0,math.hypot(dx,dy))
                midx,midy=(source[0]+target[0])/2,(source[1]+target[1])/2;ux,uy=dx/length,dy/length;slot=None
                for along,normal in ((0,14),(0,-14),(18,14),(-18,14),(18,-14),(-18,-14),(30,22),(-30,22),(30,-22),(-30,-22),(0,30),(0,-30)):
                    candidate=(midx+along*ux-normal*uy,midy+along*uy+normal*ux)
                    if chip_clear(*candidate):slot=candidate;break
                if slot:
                    mx,my=slot;occupied.append((mx-11,my-11,mx+11,my+11));edge_svg.append(f'<g class="okf-graph-export__count" data-edge-id="{_esc_attr_qs(str(data.get("id") or ""))}"><circle class="okf-graph-export__count-bg" cx="{mx:.1f}" cy="{my:.1f}" r="9"/><text class="okf-graph-export__count-label" x="{mx:.1f}" y="{my + 3:.1f}">{len(relations)}</text></g>')
                else:suppressed_chips.append(f'{data.get("source")} to {data.get("target")}: {len(relations)} relationships')
    node_svg = []
    for spec in node_specs:
        data=spec["data"];x,y,radius=spec["x"],spec["y"],spec["radius"]
        color = _sanitize_css_color(str(data.get("color") or ""))
        type_name = str(data.get("type") or "concept")
        shape = (f'<rect x="{x-radius:.1f}" y="{y-radius*.72:.1f}" width="{radius*2:.1f}" height="{radius*1.44:.1f}" rx="4"' if type_name.lower() in {"service", "api"} else f'<ellipse cx="{x:.1f}" cy="{y:.1f}" rx="{radius:.1f}" ry="{radius*.72:.1f}"' if type_name.lower() in {"table", "dataset"} else f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}"')
        short_label,label_width,label_y,label_x=spec["short"],spec["label_width"],spec["label_y"],spec["label_x"]
        fill = f' fill="{_esc_attr_qs(color)}"' if color else ' class="okf-graph-export__node-fallback"'
        node_svg.append(f'<g data-concept-id="{_esc_attr_qs(str(data["id"]))}" data-type="{_esc_attr_qs(type_name)}">{shape}{fill}/><text class="okf-graph-export__icon" x="{x:.1f}" y="{y+3:.1f}">{_esc(type_icon_key(type_name)[:1].upper())}</text><rect class="okf-graph-export__label-bg" x="{label_x:.1f}" y="{label_y-11:.1f}" width="{label_width:.1f}" height="14" rx="3"/><text class="okf-graph-export__label" x="{label_x+label_width/2:.1f}" y="{label_y:.1f}">{_esc(short_label)}</text></g>')
    return ('<svg class="okf-graph-export" data-okf-graph-export="canonical-v1" viewBox="0 0 560 250" role="img" '
            f'aria-labelledby="{_esc_attr_qs(title_id)}"><title id="{_esc_attr_qs(title_id)}">Knowledge graph overview</title>'
              f'<desc>A stable export showing {count} concepts and their in-bundle connections.{_esc(" Count equivalents: "+"; ".join(suppressed_chips)+"." if suppressed_chips else "")}</desc><defs><marker id="okf-export-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path class="okf-graph-export__arrow" d="M0,0 L7,3.5 L0,7 z"/></marker></defs>'
            f'<g class="okf-graph-thumb__edges">{"".join(edge_svg)}</g><g class="okf-graph-thumb__nodes">{"".join(node_svg)}</g></svg>')


def _graph_thumbnail_html(graph_data: dict[str, Any], href: str) -> str:
    """Wrap the shared canonical graph export for the index hero."""
    svg = graph_export_svg(graph_data)
    if not svg:
        return ('<aside class="okf-graph-thumb okf-graph-thumb--empty" aria-label="Knowledge graph overview">'
                f'<p>No concepts are available to map yet. <a href="{_esc_attr_qs(href)}">Open the graph</a>.</p></aside>')
    concept_count = len(graph_data.get("nodes", []))
    relationship_count = int(graph_data.get("raw_relationship_count") or sum(
        int(edge.get("data", {}).get("multiplicity") or 1)
        for edge in graph_data.get("edges", [])
    ))
    relationship_label = "relationship" if relationship_count == 1 else "relationships"
    # The canonical export remains a meaningful image everywhere else.  Only
    # the copy nested inside the already-named preview link is decorative, so
    # its title/description cannot pollute the link's computed name.
    decorative_svg = re.sub(
        r"<svg\b[^>]*>",
        lambda match: re.sub(
            r'\s(?:role|aria-labelledby)="[^"]*"', "", match.group(0)
        )[:-1] + ' aria-hidden="true" focusable="false">',
        svg,
        count=1,
    )
    return ('<figure class="okf-graph-thumb"><a class="okf-graph-thumb__link" '
            f'href="{_esc_attr_qs(href)}">'
            '<span class="sr-only">Open knowledge graph — </span>'
            f'{decorative_svg}'
            f'<figcaption><strong>Explore connections</strong><span>{concept_count} concepts · {relationship_count} {relationship_label}</span></figcaption></a></figure>')


def render_single_file(
    bundle: Bundle,
    out_path: Path,
    *,
    name: str | None = None,
    include_comment_summary: bool = False,
    include_portable_feedback: bool = False,
    assets: str = "cdn",
    _offline_assets: Any = None,
) -> dict:
    """Render ONE self-contained HTML file at ``out_path``.

    The bundle is embedded as a JSON blob. Cytoscape.js 3.34.3 is loaded from
    CDN by default (markdown is rendered server-side; no client-side parser);
    pass ``config.cdn = false`` via the
    bundle's ``.okf-loom/viewer/config.json`` to omit the CDN tags for fully
    offline use (you would then need to ship your own copy).

    ``include_portable_feedback`` (explicit opt-in, default off) embeds the
    per-concept exact-byte note targets and inlines the offline note composer
    (see :mod:`okf_loom.portable_feedback_export`).

    ``assets="offline"`` (explicit opt-in, X1) embeds byte-verified copies of
    the pinned Cytoscape chain, Mermaid, highlight.js and KaTeX (fonts as
    data: URLs) instead of any CDN reference, with a CDN-free CSP and the
    third-party notices; see :mod:`okf_loom.viewer.vendor`. It takes
    precedence over ``config.cdn`` and is refused while the active-code gate
    is open. The default ``"cdn"`` output is unchanged.

    Returns ``{"concepts": N, "edges": M, "bytes": K}``, plus
    ``portable_feedback_pages``/``portable_feedback_skipped`` when opted in.
    """
    from .viewer import vendor as _vendor
    _vendor.validate_mode(assets)
    out_path = Path(out_path)
    offline = None
    if assets == "offline":
        from .viewer.assets import effective_allow_active_code
        if effective_allow_active_code(bundle.root):
            raise _vendor.OfflineAssetsError(
                "--assets offline cannot be used while active code (viewer overrides + plugins) "
                "is enabled; no files changed")
        # Verify every vendored byte before any output is written.
        offline = _offline_assets if _offline_assets is not None else _vendor.load_offline_assets()
    config = load_config(bundle)
    display_name = name or config.get("name") or bundle.name

    portable_feedback = collect_portable_feedback_targets(bundle) if include_portable_feedback else None
    comment_summary = _read_bundle_comment_summary(bundle) if include_comment_summary else None
    engagement_events = _read_bundle_engagement_events(bundle)
    data = build_graph_data(bundle, name=display_name, comment_summary=comment_summary)
    nojs_relationships = "".join(
        "<li>" + _esc(relation["label"]) + " — " + _esc(relation["type"])
        + ": " + _esc(relation["source"]) + " → " + _esc(relation["target"]) + "</li>"
        for edge in data["edges"] for relation in edge["data"]["relations"]
    )
    # Every entry is a real link. `?selected=` is the deep link this file
    # already honours on load; with the viewer running the destination owner
    # selects in place instead of re-reading a multi-megabyte file.
    single_index_concepts = "".join(
        '<li><a class="okf-single-index__entry" href="?selected='
        + _esc(_url_quote("/".join(cid), safe="")) + '#detail-content"'
        + ' data-okf-single-concept="' + _esc("/".join(cid)) + '">'
        + '<strong>' + _esc(concept.title) + '</strong></a>'
        + '<span>' + _esc(concept.type or "Concept") + ' · ' + _esc("/".join(cid)) + '</span>'
        + ('<p>' + _esc(concept.description) + '</p>' if concept.description else '')
        + '</li>'
        for cid, concept in sorted(bundle.concepts.items())
    )
    if engagement_events is not None:
        data["recent_changes"] = _recent_engagement_rows(bundle, engagement_events)
    # Single-file builds have no network seam. Embed the same normalized,
    # schema-versioned passage corpus used by portable search rather than a
    # second concept-only approximation.
    data["retrieval_corpus"] = _browser_passage_corpus(bundle, data.get("edges", []))
    template = load_template("single_file.html", bundle)
    if offline is not None:
        template = offline.single_file_template(template)
    template = inject_review_metadata(template, config.get('review_base_url'))
    # Optional bounded section-destination metadata (R1/R2): a canonical
    # sibling script (not a BUNDLE key), validated once per snapshot exactly
    # like the exported workbenches. Never a change to strict schema-v1
    # passage records.
    template = template.replace(
        '</head>',
        _section_bootstrap_script(build_section_navigation(bundle))
        + '</head>', 1)
    # Independent rendered-document binding for section intents inside the
    # frozen single-file snapshot: concept identity -> per-concept
    # normalized-source revision computed from the SAME bundle source the
    # embedded bodies were rendered from. A missing or over-bound map is an
    # honest `null`: section actions then degrade to ordinary selection
    # instead of inventing a binding.
    template = template.replace(
        '</head>',
        _single_source_revisions_script(bundle)
        + '</head>', 1)
    css = (load_static("wiki.css", bundle) + "\n" + load_static("shell.css", bundle)
           + "\n" + load_static("graph.css", bundle))
    # Bundle renderers.js too so the single-file detail panel gets
    # the same mermaid/hljs/KaTeX treatment as the wiki + graph views
    # (graph.js dispatches okf-loom:bodyPatched after every showDetail).
    # theme.js is inlined in <head> (/*__THEME_HEAD_JS__*/) so it resolves
    # data-theme BEFORE first paint (no FOUC for a stored non-default
    # preference) and before capture-readiness reads it. The body bundle
    # below is preserved unchanged otherwise; theme.js is the sole owner
    # of theme state. capture-readiness.js/portable-retrieval.js/single-
    # search.js are preserved unchanged.
    theme_js = load_static("theme.js", bundle)
    js = (load_static("capture-readiness.js", bundle) + "\n" +
          load_static("portable-retrieval.js", bundle) + "\n" +
          load_static("graph.js", bundle) + "\n" +
          load_static("document-outline.js", bundle) + "\n" +
          load_static("renderers.js", bundle) + "\n" +
          load_static("single-search.js", bundle))
    if portable_feedback is not None:
        # Opt-in only: the default single-file bundle is byte-for-byte unchanged.
        js += "\n" + load_static(PORTABLE_FEEDBACK_ASSET, bundle)

    configured_theme, configured_contrast, configured_border = _theme_preference(config)
    initial_theme = _resolve_initial_theme(configured_theme)
    initial_mode = (
        "auto" if configured_theme == "auto" else configured_theme.rsplit("-", 1)[1]
    )
    revision = bundle_revision(bundle)
    data_attrs = (
        'data-okf-mode="static" data-okf-enhance="0" data-okf-studio="0" '
        f'data-okf-revision="{_esc_attr_qs(revision)}" '
        f'data-okf-capture-revision="{_esc_attr_qs(revision)}" '
        'data-okf-capture-ready="loading"'
    )
    initial_layout = config.get("default_layout") or "cose"
    cdn_scripts = _CDN_SCRIPTS if config.get("cdn", True) else (
        "<!-- CDN scripts omitted (config.cdn=false). Provide your own "
        "Cytoscape.js to enable the graph view. -->"
    )
    if offline is not None:
        # Offline carriers are placed at the first </head> of the finished
        # page below, so no body placeholder can duplicate them.
        cdn_scripts = "<!-- offline assets: embedded below (okf-vendor-*) -->"

    single_shell = render_single_file_shell(
        display_name,
        _appearance_control_html(
            initial_theme, initial_mode, configured_contrast, configured_border,
        ),
    )
    html = (
        template
          .replace("__BUNDLE_NAME__", _json_for_script(display_name))
          .replace("__BUNDLE_NAME_DISPLAY__", _esc(display_name))
          .replace("__APPLICATION_SHELL__", single_shell)
          .replace("__BUNDLE_DATA__", _json_for_script(data))
          .replace("__DATA_ATTRS__", data_attrs)
        .replace("__NOJS_RELATIONSHIPS__", nojs_relationships)
        .replace("__SINGLE_INDEX_CONCEPTS__", single_index_concepts)
        .replace("__GRAPH_THUMBNAIL__", _graph_thumbnail_html(data, "#okf-graph"))
        .replace("/*__VIEWER_CSS__*/", css)
        .replace("__THEME_CONFIG__", _theme_config_html(
            configured_theme, configured_contrast, configured_border,
        ))
        .replace("/*__THEME_HEAD_JS__*/", theme_js)
        .replace("/*__VIEWER_JS__*/", js)
        .replace("__INITIAL_THEME__", initial_theme)
        .replace("__THEME_MODIFIER_ATTRS__", _theme_modifier_attrs(
            configured_contrast, configured_border,
        ))
        .replace("__INITIAL_THEME_BUTTON__", _appearance_control_html(
            initial_theme, initial_mode, configured_contrast, configured_border,
        ))
        .replace("__INITIAL_LAYOUT__", initial_layout)
        .replace("__CDN_SCRIPTS__", cdn_scripts)
    )
    if offline is not None:
        html = html.replace(
            "</head>", offline.single_file_head(_vendor.body_needs(data)) + "</head>", 1)
    if portable_feedback is not None:
        # Injected into FINISHED output so no concept id can collide with a
        # template placeholder. The composer resolves the concept on screen
        # from the same selection signal as Review in Studio.
        html = inject_portable_feedback_bootstrap(
            html, single_file_portable_feedback_script(portable_feedback.targets))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(out_path, html)
    result = {
        "concepts": len(bundle.concepts),
        "edges": len(data["edges"]),
        "bytes": len(html.encode("utf-8")),
    }
    if portable_feedback is not None:
        result.update(portable_feedback.stats())
    return result


# ---------------------------------------------------------------------------
# Multi-file site build (spa | static | single-file)
# ---------------------------------------------------------------------------

def build_site(
    bundle: Bundle,
    out_dir: Path,
    *,
    target: str,
    name: str | None = None,
    plugin: Any = None,
    allow_active_code: bool | None = None,
    include_comment_summary: bool = False,
    include_portable_feedback: bool = False,
    output_policy: str = "replace",
    adopt_output: bool = False,
    assets: str = "cdn",
) -> dict:
    """Build a static site for the given target.

    Targets:
        "single-file" → calls :func:`render_single_file` into ``out_dir/index.html`.
        "static"      → multi-file server-rendered HTML, no JS router
                        (print-friendly, works on file://).
        "spa"         → same per-concept pages as "static" with the SPA
                        enhancements enabled (search-as-you-type, popovers).

    ``plugin`` (optional) is a viewer plugin applied to each concept page at
    render time (current spec §15). When ``None`` the bundle's
    :class:`~okf_loom.config.OkfConfig`-gated composite is built via
    :func:`okf_loom.viewer.plugins.build_viewer_plugin`.

    ``allow_active_code`` (optional, current spec §14/§15) is the
    operator-consent flag. When ``None`` it defers to the effective gate
    (bundle cfg AND operator env/CLI consent resolved via
    :func:`okf_loom.viewer.assets.effective_allow_active_code`). When the
    effective gate is open, a prominent WARNING is printed to stderr.

    ``include_portable_feedback`` (explicit opt-in, static and single-file
    only) adds the offline page-level feedback note composer, bound to each
    concept's exact on-disk bytes; skipped pages are counted in the stats.

    ``output_policy`` defaults to ``"replace"`` (unchanged legacy behaviour).
    ``"managed"`` (static and spa only) publishes a ``.okf-export.json``
    ownership manifest and refuses to replace unknown or edited output; see
    :mod:`okf_loom.export_ownership`. ``adopt_output`` requires managed mode
    and adopts an unmanaged tree only when it equals the fresh build exactly.

    ``assets`` defaults to ``"cdn"`` (unchanged output). ``"offline"`` (X1)
    ships byte-verified copies of the pinned renderer and graph libraries
    under ``__static/vendor/`` (static/spa) or embedded (single-file), with a
    CDN-free CSP, each library's LICENSE and the third-party notices files.
    Integrity is verified before anything is written; it is refused while the
    active-code gate is open or when an explicit ``plugin`` is supplied, before
    the plugin runs (:class:`~okf_loom.viewer.vendor.OfflineAssetsError`).

    Returns a stats dict with file/concept/edge counts and total bytes.
    """
    from .viewer import vendor as _vendor
    _vendor.validate_mode(assets)
    if assets == "offline" and plugin is not None:
        # A plugin is active code: it could change a page's resource set,
        # which the offline closure cannot verify. Refused before it runs.
        raise _vendor.OfflineAssetsError(
            "--assets offline cannot be used with a viewer plugin (active code); no files changed")
    out_dir = Path(out_dir)
    if target not in ("spa", "static", "single-file"):
        raise ValueError(f"Unknown target: {target!r}")
    if include_comment_summary and target == "spa":
        raise ValueError("comment summary snapshots are supported only for static and single-file targets")
    if include_portable_feedback and target == "spa":
        raise ValueError("portable feedback notes are supported only for static and single-file targets")
    if output_policy not in ("replace", "managed"):
        raise ValueError(f"Unknown output policy: {output_policy!r}")
    managed = output_policy == "managed"
    if adopt_output and not managed:
        raise ValueError("adopt_output requires output_policy='managed'")
    if managed and target == "single-file":
        raise ValueError("output_policy='managed' supports only static and spa targets")

    source = bundle.root.resolve()
    destination = out_dir.resolve()
    if source.is_relative_to(destination):
        raise ValueError("build output must not equal or contain the bundle source")
    source_documents = (*bundle.concepts.values(), *bundle.indexes.values(), *bundle.logs.values())
    if any(document.path.resolve().is_relative_to(destination) for document in source_documents):
        raise ValueError("build output must not contain loaded bundle source documents")

    stats: dict[str, Any] = {"target": target, "out_dir": str(out_dir)}
    from .io_utils import validate_tree_path
    out_dir = validate_tree_path(out_dir)
    if managed:
        # Write-free ownership preflight before any mkdir, staging or render.
        from . import export_ownership
        export_ownership.preflight(out_dir, adopt=adopt_output)

    # P2-2/P2-15 (iter-5): check effective gate + warn BEFORE the single-file
    # early return so ALL build targets emit the consent warning consistently.
    from .viewer.assets import effective_allow_active_code
    effective_allow = effective_allow_active_code(bundle.root)
    if allow_active_code is not None:
        from .viewer.assets import set_operator_consent
        set_operator_consent(bool(allow_active_code))
        effective_allow = effective_allow_active_code(bundle.root)
    offline = None
    if assets == "offline":
        if effective_allow:
            raise _vendor.OfflineAssetsError(
                "--assets offline cannot be used while active code (viewer overrides + plugins) "
                "is enabled; no files changed")
        # Verify every vendored byte before any output is created.
        offline = _vendor.load_offline_assets()
    if effective_allow:
        import sys
        print(
            f"WARNING: active code (viewer overrides + plugins) is ENABLED for "
            f"bundle {bundle.root}. Only run this for bundles whose "
            f"override/plugin sources you trust.",
            file=sys.stderr,
        )

    if target == "single-file":
        out_dir.mkdir(parents=True, exist_ok=True)
        single_kwargs: dict[str, Any] = {}
        if offline is not None:
            single_kwargs = {"assets": "offline", "_offline_assets": offline}
        single = render_single_file(
            bundle,
            out_dir / "index.html",
            name=name,
            include_comment_summary=include_comment_summary,
            include_portable_feedback=include_portable_feedback,
            **single_kwargs,
        )
        stats.update(single)
        stats["files"] = 1
        return stats

    # Multi-file (spa | static).
    config = load_config(bundle)
    display_name = name or config.get("name") or bundle.name
    palette = resolve_palette(bundle)
    comment_summary = _read_bundle_comment_summary(bundle) if include_comment_summary else None
    portable_feedback = collect_portable_feedback_targets(bundle) if include_portable_feedback else None
    engagement_events = _read_bundle_engagement_events(bundle)
    graph_data = build_graph_data(
        bundle,
        name=display_name,
        comment_summary=comment_summary,
    )
    mode = target  # passed to URL helper; spa & static share URL scheme

    # Current spec §15: load viewer plugins at render start, gated on the
    # effective allow_active_code (computed above before the single-file
    # early return). Callers may pass an explicit ``plugin``; otherwise
    # the effective-gate-driven composite is built.
    if plugin is None:
        from .viewer.plugins import build_viewer_plugin
        plugin = build_viewer_plugin(
            bundle.root, allow_active_code=effective_allow,
        )

    # Stage to a tmp dir, then atomically swap into place.
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".okf-build-", dir=str(out_dir.parent)) as tmp:
        tmp_root = Path(tmp)
        _emit_site(bundle, tmp_root, mode=mode, name=display_name,
                   palette=palette, graph_data=graph_data, config=config,
                   plugin=plugin, comment_summary=comment_summary,
                   engagement_events=engagement_events, output_destination=out_dir,
                   portable_feedback=portable_feedback, offline_assets=offline)
        verifier = None
        if managed:
            from . import LOOM_VERSION
            staged = export_ownership.seal_stage(
                tmp_root, target=target, revision=bundle_revision(bundle),
                loom_version=LOOM_VERSION)
            verifier = export_ownership.publication_verifier(
                out_dir, adopt=adopt_output, staged=staged)

        # Count this generation, not a different publisher's later output.
        total_bytes = 0
        file_count = 0
        for p in tmp_root.rglob("*"):
            if p.is_file():
                file_count += 1
                total_bytes += p.stat().st_size
        from .io_utils import atomic_replace_tree as _replace_tree
        if verifier is None:
            _replace_tree(tmp_root, out_dir)
        else:
            _replace_tree(tmp_root, out_dir, verify_previous=verifier)

    stats.update({
        "concepts": len(bundle.concepts),
        "edges": len(graph_data["edges"]),
        "files": file_count,
        "bytes": total_bytes,
    })
    if verifier is not None:
        stats.update({"output_policy": "managed", "ownership": verifier.ownership})
    if portable_feedback is not None:
        stats.update(portable_feedback.stats())
    return stats


def _apply_optional_hook(plugin: Any, hook_name: str, concept: Any, html: str) -> str:
    """P1-28: call an optional plugin hook (e.g. ``on_index_render``) if the
    plugin exposes it. Mirrors the live server's parity with the composite.
    A plugin that does not expose the hook returns ``html`` unchanged. The
    ``CompositeViewerPlugin`` already wraps hook calls in try/except; for raw
    plugins we guard here too so a raising plugin never crashes the build."""
    method = getattr(plugin, hook_name, None)
    if method is None:
        return html
    try:
        out = method(concept, html) if concept is not None else method(html)
    except Exception:
        # Best-effort: a raising optional hook is logged-and-skipped, not fatal.
        import sys
        print(f"warning: viewer plugin {hook_name} raised; skipped",
              file=sys.stderr)
        return html
    return out if isinstance(out, str) else html


def _portable_default_contexts(bundle: Bundle) -> dict[str, str]:
    """Emit canonical concept-only schema-v1 packs for no-JS static use."""
    from .retrieval import RetrievalService

    return RetrievalService(bundle).default_concept_contexts()


MAX_PORTABLE_MANIFEST_BYTES = 16 * 1024 * 1024


def _canonical_portable_manifest(manifest: dict[str, Any]) -> bytes:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_PORTABLE_MANIFEST_BYTES:
        raise ValueError("Portable search manifest exceeds the 16 MiB consumer limit.")
    return payload


def _browser_passage_corpus(bundle: Bundle, graph_edges: list[dict[str, Any]]) -> dict[str, Any]:
    """Add canonical byte strings and revision-bound graph data for browsers."""
    from .retrieval import build_passage_corpus
    from .retrieval import public_metadata
    from .retrieval import canonical_json_bytes
    from .parse import strip_markdown_for_search

    del graph_edges  # Browser retrieval needs the complete, non-visual graph.
    concepts = []
    for concept in bundle.concepts.values():
        cid = concept_id_to_str(concept.id)
        entities: list[str] = []
        for entity in concept.frontmatter.get("entities") or []:
            if isinstance(entity, str):
                entities.append(entity)
            elif isinstance(entity, dict):
                if entity.get("label"):
                    entities.append(str(entity["label"]))
                entities.extend(str(value) for value in entity.get("aliases") or []
                                if isinstance(value, str))
        metadata = public_metadata(concept.metadata)
        concepts.append({
            "id": cid,
            "title": str(concept.title or cid),
            "type": str(concept.type or "concept"),
            "path": cid + ".md",
            "tags": sorted(str(tag) for tag in concept.tags)[:20],
            "metadata": metadata,
            "metadata_json": canonical_json_bytes(metadata).decode("utf-8"),
            "search": {
                "description": str(concept.description or ""),
                "headings": [str(heading.text) for heading in concept.headings],
                "aliases": alias_labels(concept.frontmatter.get("aliases")),
                "entities": entities,
                "body": strip_markdown_for_search(concept.body or ""),
            },
        })
    concepts.sort(key=lambda row: row["id"])
    concept_ids = {row["id"] for row in concepts}
    normalized_edges = []
    for link in bundle.graph().edges:
        if link.target is None:
            continue
        source = concept_id_to_str(link.source)
        target = concept_id_to_str(link.target)
        if source == target or source not in concept_ids or target not in concept_ids:
            continue
        typed = link.origin in ("relations", "sources")
        normalized_edges.append({
            "source": source, "target": target,
            "type": str(link.label or "") if typed else "",
            "via": link.origin,
        })
    edges = sorted(
        (edge for edge in normalized_edges
         if edge["source"] in concept_ids and edge["target"] in concept_ids),
        key=lambda edge: (edge["source"], edge["target"], edge["via"], edge["type"]),
    )
    relation_rows: dict[str, list[dict[str, str]]] = {}
    for edge in edges:
        relation_rows.setdefault(edge["source"], []).append({
            "type": edge["type"], "target": edge["target"],
        })
    corpus = build_passage_corpus(bundle)
    import hashlib
    revision = corpus["manifest"]["bundle_revision"]
    for descriptor, chunk in zip(corpus["manifest"]["chunks"], corpus["chunks"]):
        for passage in chunk["passages"]:
            passage["relations"] = relation_rows.get(passage["concept_id"], [])[:20]
        payload = {
            "bundle_revision": revision,
            "count": len(chunk["passages"]),
            "index": chunk["index"],
            "passages": chunk["passages"],
            "schema_version": chunk["schema_version"],
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        identity = f"{revision}:{chunk['index']:06d}:{content_hash}"
        filename = f"passages-{chunk['index']:06d}-{content_hash[:16]}.json"
        chunk.clear()
        chunk.update({"canonical_chunk": canonical, "payload": payload})
        descriptor.update({"hash": content_hash, "id": identity, "file": filename})
    canonical_edges = json.dumps(edges, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    corpus["manifest"]["graph"] = {
        "count": len(edges),
        "hash": hashlib.sha256(canonical_edges.encode("utf-8")).hexdigest(),
        "canonical_edges": canonical_edges,
    }
    corpus["manifest"]["concept_ids"] = sorted(concept_ids)
    corpus["manifest"]["concepts"] = concepts
    _canonical_portable_manifest(corpus["manifest"])
    return corpus


def _emit_site(
    bundle: Bundle,
    root: Path,
    *,
    mode: str,
    name: str,
    palette: dict[str, str],
    graph_data: dict[str, Any],
    config: dict[str, Any],
    plugin: Any = None,
    comment_summary: CommentSummaryV1 | None = None,
    engagement_events: list[dict[str, Any]] | None = None,
    output_destination: Path | None = None,
    portable_feedback: PortableFeedbackTargets | None = None,
    offline_assets: Any = None,
) -> None:
    """Emit the full multi-file site into ``root`` (which must exist).

    ``plugin`` is an optional viewer plugin applied to each rendered concept
    page (current spec §15). It is the composite from
    :func:`~okf_loom.viewer.plugins.build_viewer_plugin` when called via
    :func:`build_site`; pass ``None`` to skip plugin processing.

    ``offline_assets`` (an already verified
    :class:`~okf_loom.viewer.vendor.OfflineAssets`) writes the vendored
    libraries to ``__static/vendor/`` and gives every page the offline CSP
    and asset bootstrap. ``None`` (default) leaves the output unchanged.
    """
    def _write_page(path: Path, html: str) -> None:
        if offline_assets is not None:
            html = offline_assets.finalize_page(html)
        _atomic_write_text(path, html)

    static_dir = root / "__static"
    static_dir.mkdir(parents=True, exist_ok=True)
    for asset_name in list_builtin_static():
        _atomic_write_text(static_dir / asset_name, load_static(asset_name, bundle))
    if portable_feedback is not None:
        # Opt-in asset: never part of the default builtin copy.
        _atomic_write_text(static_dir / PORTABLE_FEEDBACK_ASSET,
                           load_static(PORTABLE_FEEDBACK_ASSET, bundle))
    if offline_assets is not None:
        from .viewer.vendor import STATIC_VENDOR_DIR
        for rel, data in sorted(offline_assets.static_files().items()):
            target_path = static_dir.joinpath(STATIC_VENDOR_DIR, *rel.split("/"))
            target_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(target_path, data)

    data_dir = root / "__data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(data_dir / "graph.json", json.dumps(graph_data).encode("utf-8"))
    _atomic_write_bytes(data_dir / "content.json", json.dumps(_content_index_json(bundle)).encode("utf-8"))
    # Portable search corpus: fixed-name revalidated manifest plus immutable,
    # content-addressed chunks. Ordinary pages do not request either asset;
    # static-search.js is loaded only by the dedicated workbench.
    corpus = _browser_passage_corpus(bundle, graph_data.get("edges", []))
    # Optional bounded section-destination metadata for the portable
    # workbench; ``None`` (headingless bundle, over-cap) simply omits the
    # bootstrap and every result keeps document-root reading.
    section_navigation = build_section_navigation(bundle)
    portable_contexts = _portable_default_contexts(bundle)
    _atomic_write_bytes(data_dir / "passages-manifest.json",
                        _canonical_portable_manifest(corpus["manifest"]))
    for descriptor, chunk in zip(corpus["manifest"]["chunks"], corpus["chunks"]):
        _atomic_write_bytes(data_dir / descriptor["file"],
                            chunk["canonical_chunk"].encode("utf-8"))
    context_dir = root / "__context"
    for concept_id, markdown in portable_contexts.items():
        parts = concept_id.split("/")
        context_path = context_dir.joinpath(*parts[:-1], parts[-1] + ".txt")
        context_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(context_path, markdown)
    # Per-concept pages.
    graph = bundle.graph()
    for concept in bundle.concepts.values():
        # Apply plugin at body level BEFORE projection so the embedded
        # projection metadata matches the rendered body HTML (parity with
        # serve path's body_plugin seam).  Plugins that only modify body
        # content work identically in static and serve modes.
        _bp = plugin.on_concept_render if plugin is not None else None
        html = _render_concept_page(
            concept, bundle, graph, mode=mode, name=name,
            palette=palette, config=config,
            portable_context=portable_contexts.get(concept_id_to_str(concept.id)),
            body_plugin=_bp,
            comment_summary=comment_summary,
            portable_feedback=(portable_feedback.targets.get(concept_id_to_str(concept.id))
                               if portable_feedback is not None and mode == "static" else None),
        )
        cid_str = concept_id_to_str(concept.id)
        # tables/users → tables/users.html
        parts = cid_str.split("/")
        out_path = root.joinpath(*parts[:-1], parts[-1] + ".html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_page(out_path, html)

    # Directory index pages (per directory that has any concepts).
    dirs_with_concepts: set[Path] = set()
    dirs_with_concepts.update(path.parent for path in bundle.indexes if path.parent != Path('.'))
    for c in bundle.concepts.values():
        if len(c.id) > 1:
            for d in _ancestor_dirs(c.id):
                dirs_with_concepts.add(d)
    for d in sorted(dirs_with_concepts, key=lambda p: len(p.parts)):
        # Use a hand-authored index.md if present (as a directory index page),
        # else synthesize. For static emission, always emit a page at <dir>/index.html.
        rel_index_md = d / "index.md" if d != Path() else Path("index.md")
        index_file = bundle.indexes.get(rel_index_md)
        html = _render_index_page(
            bundle, mode=mode, name=name, palette=palette, config=config,
            sub=("/".join(d.parts) if d.parts else ""),
            index_file=index_file,
            comment_summary=comment_summary,
            engagement_events=engagement_events,
        )
        if plugin is not None:
            # P1-28: apply on_index_render to directory index pages too (parity
            # with the live server, which calls on_index_render at server.py).
            html = _apply_optional_hook(plugin, "on_index_render", None, html)
        out_path = (root / d / "index.html") if d.parts else (root / "index.html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_page(out_path, html)

    # Root index.html (always, even if no subdir index).
    for rel, log in bundle.logs.items():
        if not log.path.resolve().is_relative_to(bundle.root.resolve()):
            continue
        document = rel.as_posix()
        rendered = render_reserved_document(document, log.raw_text, mode=mode, config=config,
                                            bundle=bundle, name=name)
        out_path = root / rel.with_suffix('.html')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_page(out_path, rendered)

    root_index = bundle.root_index()
    html = _render_index_page(bundle, mode=mode, name=name, palette=palette,
                              config=config, sub="", index_file=root_index,
                              comment_summary=comment_summary,
                              engagement_events=engagement_events)
    if plugin is not None:
        html = _apply_optional_hook(plugin, "on_index_render", None, html)
    _write_page(root / "index.html", html)

    # Full-page graph view + search page (so internal links to them resolve).
    graph_html = _render_graph_page(bundle, mode=mode, name=name, config=config,
                                    offline_assets=offline_assets)
    _write_page(root / "__graph.html", graph_html)
    # Search results page (degraded: static has no live search backend).
    search_html = _render_search_page(
        bundle, mode=mode, name=name, config=config,
        query="", results=[],
        section_navigation=section_navigation,
    )
    _write_page(root / "__search.html", search_html)

    # Bundle-local media: copy every §5-visible media file into the site
    # tree at its bundle-relative path, so image/video/PDF references keep
    # working in exported sites exactly as the live server serves them.
    _copy_bundle_media(bundle, root, output_destination=output_destination)


def _copy_bundle_media(bundle: Bundle, root: Path, *, output_destination: Path | None = None) -> int:
    """Copy §5-visible bundle media into the site output; returns the count.

    Same visibility semantics as the live server's media route
    (``iter_bundle_files`` over ``BUNDLE_MEDIA_EXTENSIONS`` with the
    bundle's exclude/include/gitignore config) and the same containment
    guard (a symlinked file pointing outside the bundle root is skipped,
    P2-54). Files under a top-level ``__``-prefixed directory are skipped —
    ``__static``/``__data`` are the site's own namespaces.
    """
    import posixpath as _pp
    from .config import OkfConfig, OkfConfigError
    from .ignore import iter_bundle_files, path_within
    from .viewer.assets import BUNDLE_MEDIA_EXTENSIONS
    try:
        bundle_cfg = OkfConfig.load(bundle.root).bundle
    except OkfConfigError:
        from .config import BundleConfig
        bundle_cfg = BundleConfig()
    media = iter_bundle_files(
        bundle.root,
        name_predicate=lambda n: _pp.splitext(n.lower())[1] in BUNDLE_MEDIA_EXTENSIONS,
        exclude=bundle_cfg.exclude,
        include=bundle_cfg.include,
        respect_gitignore=bundle_cfg.respect_gitignore,
    )
    copied = 0
    # The destination and this invocation's staging tree are derived output,
    # not media input, even when bundle.include explicitly traverses them.
    # Keep the shared §5 scanner's selection/containment behavior otherwise.
    output_roots = [root.resolve()]
    if output_destination is not None:
        output_roots.append(output_destination.resolve())
    for src in media:
        rel = src.relative_to(bundle.root)
        if rel.parts and rel.parts[0].startswith("__"):
            continue
        if not path_within(src, bundle.root):
            continue
        if any(src.resolve().is_relative_to(output) for output in output_roots):
            continue
        dest = root.joinpath(*rel.parts)
        try:
            data = src.read_bytes()
        except OSError as e:
            import sys
            print(f"warning: skipping unreadable media file {src}: {e}",
                  file=sys.stderr)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(dest, data)
        copied += 1
    return copied


def _ancestor_dirs(cid: ConceptId) -> list[Path]:
    out: list[Path] = []
    for i in range(len(cid) - 1):
        out.append(Path(*cid[:i + 1]))
    return out


def _content_index_json(bundle: Bundle) -> dict[str, Any]:
    """Compact content index suitable for external tools to consume."""
    return {
        "name": bundle.name,
        "concepts": [
            {
                "id": concept_id_to_str(c.id),
                "type": c.type,
                "title": c.title,
                "description": c.description,
                "tags": list(c.tags),
                "resource": c.resource,
                "timestamp": c.timestamp,
                "metadata": c.metadata,
                "generated_at": c.generated_at,
                "path": str(c.rel_path),
            }
            # iter3 P2-1: sort by concept_id (§3.2 determinism).
            for c in sorted(bundle.concepts.values(), key=lambda c: c.id)
        ],
    }


def _concept_aliases(c: Concept) -> list[str]:
    """Frontmatter ``aliases`` (SPEC §7 governed key) as a list of strings.

    Tolerates missing/scalar/list shapes (mirrors :py:attr:`Concept.tags`).
    Producers MAY omit ``aliases``; consumers MUST treat it as ``[]`` then.
    """
    return alias_labels(c.frontmatter.get("aliases"))


def _search_corpus_json(bundle: Bundle) -> list[dict[str, Any]]:
    """Build-time search corpus for the static client-side searcher.

    One entry per concept, sorted by concept_id (current spec §3 determinism).
    The shape matches what ``static-search.js`` consumes: a bare JSON array
    of ``{id, title, description, type, tags, aliases, body_excerpt}``
    records. ``body_excerpt`` is the first ~500 chars of
    :func:`strip_markdown_for_search` output (stable, low-noise text).

    Used only by the ``--target static`` build path; ``serve``/``spa`` keep
    using the live ``/__search`` backend. Mirrors the lexical-backend field
    set so live and static results stay semantically close.
    """
    from .parse import strip_markdown_for_search
    out: list[dict[str, Any]] = []
    for c in bundle.concepts.values():
        body_clean = strip_markdown_for_search(c.body or "")
        out.append({
            "id": concept_id_to_str(c.id),
            "title": c.title,
            "description": c.description,
            "type": c.type or "",
            "tags": list(c.tags),
            "aliases": _concept_aliases(c),
            "body_excerpt": body_clean[:500],
        })
    out.sort(key=lambda e: e["id"])
    return out


# ---------------------------------------------------------------------------
# Page rendering (shared with server.py via _render_* helpers)
# ---------------------------------------------------------------------------

def _demote_headings(html: str) -> str:
    """Demote all HTML headings in ``html`` by one level (h1→h2, h2→h3, … h6→h6).

    Used on concept-page body HTML so the page <h1> (the concept title) is
    the only top-level heading, preserving a clean document outline. Runs
    right-to-left to avoid double-demoting (h1→h2 would then match h2→h3).
    Operates on <hN> and </hN> tags; attribute-safe because the regex
    requires the digit immediately after ``h`` and matches the closing
    slash in self-closing variants.
    """
    import re as _re
    out = html
    for level in range(6, 0, -1):
        new = min(level + 1, 6)
        out = _re.sub(
            rf"<h{level}([ >])", f"<h{new}\\1", out
        )
        out = _re.sub(
            rf"</h{level}>", f"</h{new}>", out
        )
    return out


def _esc(s: Any) -> str:
    """HTML-escape a value for safe interpolation into the page."""
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# Phase 2 search chip type-icon parity (plan §3.4): the live-suggestions
# combobox renders a type ICON + tinted chip per result. The icon needs the
# same SVG path map the server uses everywhere else. We ship it once per page
# via a non-executed <script type="application/json"> block (CSP-safe: a JSON
# data block is not governed by script-src) that wiki.js reads on boot. This
# keeps Python's :func:`type_icon_paths` the SINGLE source of truth — no JS
# duplicate to drift — and gives static, spa, and serve builds identical
# suggestion chip icons. The block is small (14 entries × ~60 chars).
_TYPE_ICONS_BLOCK_CACHE: str | None = None
def _type_icons_block() -> str:
    """JSON data block shipping the icon-key → SVG-inner-markup map."""
    global _TYPE_ICONS_BLOCK_CACHE
    if _TYPE_ICONS_BLOCK_CACHE is None:
        payload = json.dumps(type_icon_paths(), separators=(",", ":"))
        # Escape so the payload is safe to embed inside <script type="application/json">:
        # the JSON itself contains '<' (SVG path data) which would otherwise be a
        # literal-less blocker for HTML parsers inside <script>.
        payload = payload.replace("<", "\\u003c").replace("&", "\\u0026").replace(">", "\\u003e")
        _TYPE_ICONS_BLOCK_CACHE = (
            '<script type="application/json" id="okf-type-icons">'
            + payload + '</script>\n'
        )
    return _TYPE_ICONS_BLOCK_CACHE


def _static_prefix_for(mode: str, source_cid: ConceptId | None = None) -> str:
    """URL prefix for ``/__static`` from a given source path."""
    if mode in ("serve", "spa"):
        return "/__static"
    # static: relative path back to the root.
    if source_cid is None or len(source_cid) <= 1:
        return "__static"
    depth = len(source_cid) - 1
    return "/".join([".."] * depth) + "/__static"


def _root_prefix_for(mode: str, source_cid: ConceptId | None = None) -> str:
    """URL prefix for the bundle root from a given source path.

    P1-3 (iter-4): serve/spa must return ``"/"`` (root-absolute), not ``""``
    (directory-relative). With ``""``, nested concepts at ``/tables/orders``
    resolve nav links (search/graph/index) to ``/tables/__search`` → 404.
    The brand link already special-cases this (render.py); the nav controls
    must match.
    """
    if mode in ("serve", "spa"):
        return "/"
    if source_cid is None or len(source_cid) <= 1:
        return "./"
    depth = len(source_cid) - 1
    return "/".join([".."] * depth) + "/"


def _render_link_map(bundle: Bundle, concept: Concept, mode: str) -> dict[str, str | None]:
    """Build a target_raw → url map for the concept's outgoing links.

    Keys are ANCHOR-STRIPPED: ``rewrite_internal_links`` looks hrefs up
    with the ``#fragment`` removed (and re-appends it to the mapped URL),
    so a map keyed by the full ``cli.md#validate`` target_raw would never
    match — anchored internal links then silently stayed raw and 404'd in
    static builds (the live server resolves raw .md paths, which masked
    this).
    """
    out: dict[str, str | None] = {}
    for link in bundle.graph().out_edges.get(concept.id, []):
        key = link.target_raw.split("#", 1)[0]
        if not key:
            continue  # pure-anchor link; not a rewrite candidate
        if link.target is None:
            out.setdefault(key, None)
            continue
        out[key] = url_for_concept(
            link.target, mode, source_cid=concept.id,
        )
    return out


# <img> tags come from our own renderer (_img_tag) with a double-quoted
# src, so this match is reliable. ``(?!/)`` skips protocol-relative ``//``
# (already neutralised by _safe_url, but keep the guard local too).
_IMG_ABS_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=")/(?!/)([^"]+)(")')


def _relativize_asset_srcs(html: str, root_prefix: str) -> str:
    """Rewrite absolute bundle-relative ``<img src="/...">`` to root-relative.

    Static builds are browsed from ``file://`` or under arbitrary path
    prefixes (GitHub Pages project sites), so an absolute src — the
    recommended authoring form — must become relative to the page's
    directory, mirroring what :func:`url_for_concept` does for links.
    serve/spa keep absolute srcs (both are rooted at ``/``). Relative srcs
    (``./assets/x.png``) already resolve correctly against the page path
    and pass through untouched.
    """
    return _IMG_ABS_SRC_RE.sub(
        lambda m: m.group(1) + root_prefix + m.group(2) + m.group(3), html
    )


def _static_md_href(href: str, root_prefix: str) -> str | None:
    """Map an internal ``/a/b.md`` href to its static-build page URL.

    Static builds contain only ``.html`` files and are served under an
    arbitrary path prefix (e.g. GitHub Pages project sites), so a
    bundle-absolute raw-file href would 404 twice over. Returns
    ``<root_prefix>a/b.html`` for internal bundle-absolute ``.md`` hrefs,
    None for anything else (external URLs, anchors, non-md paths).
    """
    if not (href.startswith("/") and href.endswith(".md")):
        return None
    return root_prefix + href[1:-3] + ".html"


def _index_body_link_map(bundle: Bundle, body_md: str, mode: str) -> dict[str, str | None]:
    """target_raw → url map for the root ``index.md`` body.

    The root index is a reserved file, not a concept, so it has no graph
    out-edges; extract and resolve its links directly. External /
    out-of-bundle / anchor-only links are left untouched (absent from the
    map); internal links resolve to the mode URL — or None (rendered with
    ``data-okf-broken``) — exactly like concept bodies do via
    :func:`_render_link_map`. The index page sits at the bundle root, so
    static URLs need no ``source_cid`` relativisation.
    """
    from .parse import extract_links
    out: dict[str, str | None] = {}
    for link in extract_links(body_md, source_dir=bundle.root, bundle_root=bundle.root):
        if link.form in ("external", "external_out_of_bundle", "anchor"):
            continue
        key = link.target_raw.split("#", 1)[0]  # match _render_link_map keying
        if not key:
            continue
        cid = link.concept_id
        if cid is not None and cid in bundle.concepts:
            out[key] = url_for_concept(cid, mode)
            continue
        # Links to reserved sub-index files (`explanation/index.md`) are
        # not concepts but DO have a page: map them to the sub-index URL
        # in static builds; the live server already resolves them (serve
        # renders /<sub>/index.md directly), so leave them alone there.
        if cid is not None and cid[-1] == "index" and len(cid) > 1:
            sub_dir = "/".join(cid[:-1])
            if (bundle.root / sub_dir).is_dir():
                if mode == "static":
                    out[key] = f"{sub_dir}/index.html"
                continue
        out.setdefault(key, None)
    return out


def _readtime_html(body: str) -> str:
    """Estimated reading time chip for the concept header (~220 wpm).

    Hidden for stub-length bodies (< half a minute of reading) where
    "1 min read" would be noise rather than signal.
    """
    words = len((body or "").split())
    if words < 110:
        return ""
    minutes = max(1, round(words / 220))
    return f'<span class="okf-readtime">{minutes} min read</span>'


def _type_base_slug(type_name: str) -> str:
    """Human-readable base slug for a type name (lowercased, hyphenated).

    Whitespace is stripped first so ``" A "`` and ``"A"`` produce the same
    base slug. Non-alphanumeric runs collapse to ``-``. The result is a
    valid HTML-id fragment (letters, digits, hyphens only).
    """
    raw = (type_name or "").strip()
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "type"


def _allocate_type_section_ids(types: list[str]) -> dict[str, str]:
    """Deterministic, GUARANTEED-globally-unique HTML-id allocation for type
    sections.

    Given the full type list, returns ``{type_name: section_id}`` where every
    id is unique — no probabilistic hash, and no cross-base-suffix collision.

    Collisions are resolved with a GLOBAL ``used`` set plus linear probing:
    for each type (in sorted order), start with the bare base slug; if that
    candidate is already taken (by ANY previous type, not just same-base
    siblings), try ``base-2``, ``base-3``, etc. until a free id is found.

    This is globally collision-safe: for input ``['a', 'a!', 'a-2']`` the
    types slug to bases ``a``, ``a``, ``a-2``. Allocation:
      - ``'a'``    → candidate ``a``      (free) → ``a``
      - ``'a!'``   → candidate ``a`` taken → ``a-2`` (free) → ``a-2``
      - ``'a-2'``  → candidate ``a-2`` taken → ``a-2-2`` (free) → ``a-2-2``
    No duplicates.

    Deterministic: the same sorted type list always produces the same id
    mapping. Whitespace is stripped before slug computation.
    """
    used: set[str] = set()
    result: dict[str, str] = {}
    for t in sorted(types):
        base = _type_base_slug(t)
        candidate = base
        n = 2
        while candidate in used:
            candidate = f"{base}-{n}"
            n += 1
        used.add(candidate)
        result[t] = candidate
    return result


def render_concept_body_with_projection(
    concept: Concept, bundle: Bundle, *, mode: str, root_prefix: str = "",
    body_plugin: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Render body HTML and its canonical projection through one parity seam.

    Renders with owner markers assigned to each projectable block **before**
    pre-plugin transforms (link rewrite, citation strip, heading demotion) and
    the plugin. The finalizer verifies every eligible owner survives exactly
    once with exact visible text, then replaces markers with numeric keys.

    If *body_plugin* is provided it is called as ``body_plugin(concept, html)``
    on the body HTML **before** projection finalization, so initial-page
    and /__data/doc share one plugin-then-projection seam (ReviewAll r2/r3).
    """
    from .viewer.markdown import render_blocks_with_owners
    # The model parser normalizes CRLF while the annotation contract counts
    # exact canonical code points. Render the actual body suffix so owner spans
    # share the extractor's coordinate space; do not guess normalized offsets.
    render_body = concept.body
    source = concept.raw_text
    if '\r\n' in source:
        from .parse import parse_document
        if parse_document(source)[1] == concept.body:
            lines = source.splitlines(keepends=True)
            start = 0
            if lines and lines[0].strip() == '---':
                end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
                start = sum(len(line) for line in lines[:end + 1])
                if end + 1 < len(lines) and not lines[end + 1].strip():
                    start += len(lines[end + 1])
            render_body = source[start:]
    # 1. Render with owner markers (before any transform/plugin).
    body_html, owners = render_blocks_with_owners(render_body)
    # 2. Pre-plugin transforms (owner markers survive: they are attributes on
    #    the block's containing element; href/src/heading-level changes do not
    #    touch data-okf-proj-owner).
    body_html = rewrite_internal_links(body_html, _render_link_map(bundle, concept, mode))
    body_html = normalize_document_links(
        body_html, concept_id_to_str(concept.id) + '.md', mode,
        page_url=url_for_concept(concept.id, mode, source_cid=concept.id))
    body_html = join_source_footnotes(body_html, concept)
    if mode == "static":
        body_html = _relativize_asset_srcs(body_html, root_prefix)
    _citation_stripped = False
    if _has_frontmatter_citations(concept):
        body_html = _strip_body_citations_section(body_html)
        _citation_stripped = True
    body_html = _demote_headings(body_html)
    # Private pre-plugin proof, independently decorated just like final output.
    # Do not move the plugin hook or derive this view from plugin-returned HTML.
    trusted_html = _decorate_content_scrollports(body_html)
    # 3. Plugin runs after owner markers are in place.
    if body_plugin is not None:
        body_html = body_plugin(concept, body_html)
    body_html = _decorate_content_scrollports(body_html)
    # 4. Finalize: verify owners, replace with numeric keys, build projection.
    return build_annotation_projection(concept.raw_text, render_body, body_html,
                                       owners=owners, citation_stripped=_citation_stripped,
                                       _trusted_html=trusted_html)


def _render_concept_page(
    concept: Concept,
    bundle: Bundle,
    graph,
    *,
    mode: str,
    name: str,
    palette: dict[str, str],
    config: dict[str, Any],
    portable_context: str | None = None,
    body_plugin: Any = None,
    comment_summary: CommentSummaryV1 | None = None,
    portable_feedback: dict[str, str] | None = None,
) -> str:
    template = load_template("concept_page.html", bundle)
    if mode != 'serve':
        template = inject_review_metadata(template, config.get('review_base_url'), concept_id_to_str(concept.id) + '.md')
    cid_str = concept_id_to_str(concept.id)
    static_prefix = _static_prefix_for(mode, concept.id)
    root_prefix = _root_prefix_for(mode, concept.id)

    # Initial pages and /__data/doc share this exact rendering/projection seam.
    body_html, annotation_projection = render_concept_body_with_projection(
        concept, bundle, mode=mode, root_prefix=root_prefix,
        body_plugin=body_plugin,
    )
    # R1/R2 destination-side admission proof: the canonical concept identity,
    # the concept's per-document normalized-source revision, and the
    # renderer-owned heading map travel with the document, so a section
    # intent can be admitted against the DISPLAYED bytes — a stale search
    # page linking into a re-rendered document, an intent minted for another
    # concept, or a script that moved or renamed a heading, fails closed to
    # document-root reading. The revision is concept_content_rev of THIS
    # concept's normalized raw source (the canonical source the renderer
    # consumed) — never served disk bytes, never the corpus revision.
    from .annotations import concept_content_rev
    template = template.replace(
        '</head>',
        _rendered_outline_script(
            body_html, cid_str,
            concept_content_rev(concept.raw_text.replace("\r\n", "\n")))
        + '</head>', 1)

    palette_for = palette
    type_color = palette_for.get(concept.type or "", type_color_for(concept.type))

    # Frontmatter table (exclude governed keys that get dedicated sections).
    _GOVERNED_KEYS = frozenset({"aliases", "entities", "provenance", "citations", "relations"})
    # P2-11 (iter-3): also exclude keys already rendered in the page header
    # (type chip / h1 / description paragraph / resource link / tags row).
    # The "All fields" details block promises completeness but mostly
    # repeated what the user just read; only timestamp + unknown/extra keys
    # are genuinely new information in the table. Forward-compat value
    # (unknown producer-added keys surfacing here) is preserved.
    _HEADER_RENDERED_KEYS = frozenset({
        "type", "title", "description", "resource", "tags",
    })
    fm_rows: list[str] = ["<table><tbody>"]
    for k, v in concept.frontmatter.items():
        if k in _GOVERNED_KEYS or k in METADATA_FIELDS:
            continue  # rendered in dedicated sections below
        if k in _HEADER_RENDERED_KEYS:
            continue  # already rendered in the page header (P2-11)
        fm_rows.append(
            f'<tr><th>{_esc(k)}</th><td><code>{_esc(_fmt_fm_value(v))}</code></td></tr>'
        )
    fm_rows.append("</tbody></table>")
    frontmatter_html = "\n".join(fm_rows) if concept.frontmatter else "<p class='okf-muted'>(none)</p>"

    # Dedicated sections for governed frontmatter keys.
    governed_html = _render_governed_keys(concept, bundle, mode=mode, root_prefix=root_prefix)

    # Resource.
    resource_html = ""
    if concept.resource:
        # Route through _safe_url to prevent javascript:/data: XSS via the
        # resource frontmatter key (same defence as markdown link hrefs).
        from .viewer.markdown import _safe_url
        safe_resource = _safe_url(concept.resource)
        if safe_resource is not None:
            # Static builds have no server to render raw bundle paths, so
            # an internal /a/b.md resource must point at its built page.
            static_href = _static_md_href(safe_resource, root_prefix)
            if mode == "static" and static_href is not None:
                safe_resource = static_href
            resource_html = (
                f'<a class="okf-external" href="{_esc(safe_resource)}" '
                f'target="_blank" rel="noopener noreferrer">{_esc(concept.resource)}</a>'
            )
        else:
            resource_html = (
                f'<span class="okf-link-blocked" title="blocked: '
                f'{_esc(concept.resource)}">{_esc(concept.resource)}</span>'
            )

    # Tags.
    tags_html = "".join(
        f'<span class="okf-tag">{_esc(t)}</span>' for t in concept.tags
    )

    # Backlinks ("Cited by").
    backlinks = graph.backlinks(concept.id)
    backlinks_html = _render_link_list(
        [(b.source, b.label, mode, concept.id, b.target_raw, b.origin) for b in backlinks],
        bundle, kind="back", comment_summary=comment_summary,
    )

    # Outgoing links.
    out_links = graph.outlinks(concept.id)
    outgoing_html = _render_link_list(
        [(l.target, l.label, mode, concept.id, l.target_raw, l.origin) for l in out_links if l.target is not None],
        bundle, kind="out", comment_summary=comment_summary,
    )

    # Local graph widget data (1-hop neighbours + self).
    # CRITICAL: the current concept MUST be nodes[0] so the client-side SVG
    # renderer (wiki.js renderLocalGraph) draws it as the centered node.
    local_ids = {concept.id} | set(graph.neighbours(concept.id, max_depth=1))
    current_node = {
        "id": concept_id_to_str(concept.id),
        "label": concept.title,
        "type": concept.type or "concept",
        "color": palette_for.get(concept.type or "", type_color_for(concept.type)),
        "is_center": True,
    }
    neighbour_nodes = [
        {
            "id": concept_id_to_str(c.id),
            "label": c.title,
            "type": c.type or "concept",
            "color": palette_for.get(c.type or "", type_color_for(c.type)),
            "is_center": False,
        }
        for c in sorted(bundle.concepts.values(), key=lambda c: c.id)
        if c.id in local_ids and c.id != concept.id
    ]
    local_nodes = [current_node] + neighbour_nodes
    local_edges = [
        {"source": concept_id_to_str(e.source), "target": concept_id_to_str(e.target)}
        # iter2 P2-1: sort for deterministic local-graph JSON (§3.2).
        for e in sorted(graph.edges, key=lambda e: (e.source, e.target if e.target is not None else ()))
        if e.source in local_ids and e.target in local_ids
    ]
    local_data = json.dumps({"nodes": local_nodes, "edges": local_edges})

    # Topbar nav (P2-61: shared fragment so chrome is identical across
    # concept/index/search pages; Appearance control matches initial theme
    # to avoid FOUC). theme.js is the sole owner of theme state; the server
    # only seeds the first paint.
    # P1-3: in static mode the Graph link must point at __graph.html (the
    # page is emitted at that path; the extensionless URL 404s).
    configured_theme, configured_contrast, configured_border = _theme_preference(config)
    initial_theme = _resolve_initial_theme(configured_theme)
    initial_mode = (
        "auto" if configured_theme == "auto" else configured_theme.rsplit("-", 1)[1]
    )
    nav_html = _nav_controls_html(
        root_prefix=root_prefix,
        graph_link=f"{root_prefix}__graph{'.html' if mode == 'static' else ''}",
        initial_theme=initial_theme,
        initial_mode=initial_mode,
        initial_contrast=configured_contrast,
        initial_border=configured_border,
        mode=mode,
    )
    bundle_href = "/" if mode in ("serve", "spa") else root_prefix
    shell_html = _application_shell_html(
        name=name,
        bundle_href=bundle_href,
        search_href=f"{root_prefix}__search{'.html' if mode == 'static' else ''}",
        index_href=bundle_href,
        graph_href=f"{root_prefix}__graph{'.html' if mode == 'static' else ''}",
        current="index",
        mode=mode,
        initial_theme=initial_theme,
        initial_mode=initial_mode,
        initial_contrast=configured_contrast,
        initial_border=configured_border,
    )

    theme_attr = (f' data-theme="{initial_theme}"'
                  + _theme_modifier_attrs(configured_contrast, configured_border))
    revision = bundle_revision(bundle)
    # Capture readiness is deliberately not server-seeded on document
    # surfaces. An absent surface state is nonterminal to
    # capture-readiness.js; the deferred renderer and (serve only) Studio
    # must publish their own terminal state before a capture can pass.
    data_attrs = (
        f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}" '
        f'data-okf-studio="{("1" if mode == "serve" else "0")}" '
        f'data-okf-concept="{_esc_attr_qs(cid_str)}" data-okf-portable-root="{_esc_attr_qs(root_prefix)}" '
        f'data-okf-source-mapping="schema-v1" data-okf-revision="{_esc_attr_qs(revision)}" '
        f'data-okf-capture-revision="{_esc_attr_qs(revision)}"'
    )

    css_link = f'<link rel="stylesheet" href="{static_prefix}/wiki.css">'
    shell_css_link = f'<link rel="stylesheet" href="{static_prefix}/shell.css">'
    # theme.js is parser-blocking (no defer) in <head> so it resolves
    # data-theme BEFORE first paint (no FOUC for a stored non-default
    # preference) and before capture-readiness.js reads it; the rest stay
    # deferred at end of <body>. theme.js is the SOLE owner of theme state.
    # On serve, server.py injects the studio bootstrap JSON + theme.js into
    # <head> (so theme.js can read studio.theme); on static/spa the template
    # emits theme.js from __THEME_HEAD_JS__.
    head_theme_js = (
        "" if mode == "serve"
        else f'<script src="{static_prefix}/theme.js"></script>'
    )
    js_link = (
        f'<script src="{static_prefix}/capture-readiness.js" defer></script>\n'
        f'<script src="{static_prefix}/portable-retrieval.js" defer></script>\n'
        f'<script src="{static_prefix}/wiki.js" defer></script>'
    )
    document_outline_link = f'<script src="{static_prefix}/document-outline.js" defer></script>'
    renderers_link = f'<script src="{static_prefix}/renderers.js" defer></script>'
    # Opt-in portable feedback (static only): the composer loads after the
    # renderers; pages without an exact-byte target get neither piece.
    portable_feedback = portable_feedback if mode == "static" else None
    if portable_feedback is not None:
        renderers_link += f'\n<script src="{static_prefix}/{PORTABLE_FEEDBACK_ASSET}" defer></script>'

    # P2-22: render aliases as a subtitle paragraph immediately after the
    # page title (SPEC §7.1 wording: "show as subtitles"). The subtitle is
    # emitted as a standalone placeholder so the template can interpolate
    # it directly after ``__CONCEPT_TITLE__``.
    subtitle_html = _render_subtitle(concept)

    context_href = f'/__context?concept={_esc_attr_qs(cid_str)}&format=markdown'
    # R3: a Context Pack is cited evidence, not a summary and not a message to
    # the agent. The description is the action's own, so it is said wherever
    # the action is reached (not only on the Search page). The note is hidden:
    # it is read as the action's description, not again as page text.
    context_note = (
        'Cited passages for reuse, not a summary. Copying sends nothing to the agent.'
    )
    context_action_html = (
        f'<a class="okf-btn okf-context-copy" data-context-concept="{_esc_attr_qs(cid_str)}" '
        f'aria-describedby="okf-context-action-note" href="{context_href}">Open context pack</a>'
        f'<span id="okf-context-action-note" hidden>{context_note}</span>'
    )
    if mode in ("static", "spa"):
        local_href = f'{root_prefix}__context/{cid_str}.txt'
        context_action_html = (
            f'<button type="button" class="okf-btn okf-context-copy okf-js-action" data-context-concept="{_esc_attr_qs(cid_str)}" '
            'aria-describedby="okf-context-action-note">Copy concept context</button>'
            f'<span id="okf-context-action-note" hidden>{context_note}</span>'
            '<details class="okf-context-manual-options"><summary>More context options</summary>'
            f'<a class="okf-btn" href="{_esc_attr_qs(local_href)}" '
            f'aria-label="Open canonical concept-only context pack for {_esc_attr_qs(concept.title)}">'
            'Open concept-only context pack</a>'
            '<small class="okf-context-scope-note">Selecting exact rendered text loads portable evidence only when needed.</small>'
            '</details>'
        )

    # P2-3 (iter-1): clickable breadcrumb trail (replaces the flat raw id).
    breadcrumb_html = _render_breadcrumb(concept, mode=mode, name=name)

    # Concept anatomy. Entity chips go in the header. Document details
    # (facts + tags + governed keys) follows the authored reading region,
    # OUTSIDE .okf-page__body to preserve projection parity. Connections and
    # prev/next follow.
    entity_chips_html = _render_entity_chips(concept, mode=mode, root_prefix=root_prefix)
    long_document = len(_ONTHISPAGE_HEADING_RE.findall(body_html)) >= 8
    infobox_html = _render_infobox(
        concept,
        type_color=type_color,
        revision=revision,
        tags_html=tags_html,
        frontmatter_html=frontmatter_html,
        governed_html=governed_html,
        long_document=long_document,
    )
    connections_html = _render_connections(outgoing_html, backlinks_html)
    prevnext_html = _render_prevnext(concept, bundle, mode=mode)
    # Shared near-header outline: parse rendered heading IDs, not
    # concept.headings, so links match the DOM (the renderer and
    # parse.extract_headings use slightly different slugify rules). Always
    # emit the host; headingless bodies receive a hidden empty shell that
    # document-outline.js can revive after a supported body patch.
    onthispage_html = _render_onthispage(body_html)
    document_end_html = (
        '<nav class="okf-document-end" aria-label="Document end">'
        '<a href="#okf-document-details">Back to document details</a>'
        f'<a href="{_esc_attr_qs(bundle_href)}">Return to Index</a>'
        '</nav>'
    )

    rendered = (
        template
        .replace("__LANG__", "en")
        .replace("__THEME_ATTR__", theme_attr)
        .replace("__DATA_ATTRS__", data_attrs)
        .replace("__HEAD_TITLE__", _esc(f"{concept.title} — {name}"))
        .replace("__BUNDLE_NAME__", _esc(name))
        .replace("__NAV_HTML__", nav_html)
        .replace("__APPLICATION_SHELL__", shell_html)
        .replace("__STATIC_PREFIX__", static_prefix)
        # P1-8: brand link must point to the bundle root for ALL modes.
        # _root_prefix_for returns "" for serve/spa (which is correct for
        # the relative nav URLs the server resolves), but the brand link
        # needs an explicit "/" there. For static we use the relative
        # root_prefix ("./" or "../../"+).
        .replace("__BRAND_HREF__", _esc_attr_qs("/" if mode in ("serve", "spa") else root_prefix))
        # P1-9: local-graph pill nav reads data-root-prefix to build the
        # target URL. Only consumed by wiki.js in static mode.
        .replace("__ROOT_PREFIX__", _esc_attr_qs(root_prefix))
          .replace("__WIKI_CSS_LINK__", css_link)
          .replace("__SHELL_CSS_LINK__", shell_css_link)
          .replace("__THEME_CONFIG__", _theme_config_html(
              configured_theme, configured_contrast, configured_border,
          ))
          .replace("__THEME_HEAD_JS__", head_theme_js)
        .replace("__WIKI_JS_LINK__", _type_icons_block() + js_link)
        .replace("__DOCUMENT_OUTLINE_JS_LINK__", document_outline_link)
        .replace("__RENDERERS_JS_LINK__", renderers_link)
        .replace("__CONCEPT_ID__", _esc(cid_str))
        .replace("__BREADCRUMB_HTML__", breadcrumb_html)
        .replace("__CONCEPT_TYPE__", _esc(concept.type or "concept"))
        .replace("__CONCEPT_TYPE_COLOR__", _esc(type_color))
        .replace("__CONCEPT_TYPE_FG__", _esc(_chip_fg(type_color)))
        .replace("__TYPE_ICON__", type_icon_svg(concept.type, size=18))
        .replace("__READTIME_HTML__", _readtime_html(concept.body))
        # P1-1 (iter-2): the subtitle is a SIBLING <p> after the h1, not a
        # child of it. The prior concatenation into __CONCEPT_TITLE__ produced
        # invalid HTML (<h1> content model is phrasing-only; <p> is flow) and
        # polluted the h1's accessible name with the alias list on every
        # aliased concept. subtitle_html is "" when there are no aliases, so
        # the placeholder renders nothing in that case.
        .replace("__CONCEPT_TITLE__", _esc(concept.title))
         .replace("__CONCEPT_SUBTITLE__", subtitle_html)
         .replace("__CONTEXT_ACTION_HTML__", context_action_html)
        .replace("__ENTITY_CHIPS_HTML__", entity_chips_html)
        .replace("__CONCEPT_DESCRIPTION__", _esc(concept.description))
        .replace("__CONCEPT_RESOURCE__", resource_html)
        # Phase 2: tags moved to the infobox; the header meta row now holds
        # only the resource link. __CONCEPT_TAGS__ is kept as a no-op for
        # any downstream template that still references it.
        .replace("__CONCEPT_TAGS__", "")
        .replace("__INFOBOX_HTML__", infobox_html)
        .replace("__METADATA_HTML__", render_metadata(concept, bundle, mode=mode))
          .replace("__ONTHISPAGE_HTML__", onthispage_html)
          .replace("__CONCEPT_BODY__", body_html)
          .replace("__CANONICAL_SOURCE__", base64.b64encode(concept.raw_text.encode("utf-8")).decode("ascii"))
          .replace("__ANNOTATION_PROJECTION__", json.dumps(annotation_projection, separators=(",", ":")).replace("<", "\\u003c").replace("&", "\\u0026"))
        .replace("__CONNECTIONS_HTML__", connections_html)
        .replace("__PREVNEXT_HTML__", prevnext_html)
        .replace("__DOCUMENT_END_HTML__", document_end_html)
        # Phase 2: the old separate placeholders are absorbed into the
        # infobox/connections builders above. These replaces are kept as
        # no-ops for backward compatibility with any external template.
        .replace("__BACKLINKS_HTML__", backlinks_html)
        .replace("__OUTGOING_HTML__", outgoing_html)
        .replace("__FRONTMATTER_HTML__", frontmatter_html)
        .replace("__GOVERNED_HTML__", governed_html)
        .replace("__LOCAL_GRAPH_DATA__", _esc(local_data))
    )
    # P2-68: strip empty ``<section class="okf-relations__block">`` blocks
    # (those whose only content was an empty link list + the heading).
    # Phase 2: the template no longer emits okf-relations__block sections,
    # so this regex is a harmless no-op on the new markup; it stays for any
    # external/legacy template that still uses the old relations nav.
    rendered = _EMPTY_RELATIONS_SECTION_RE.sub("", rendered)
    if portable_feedback is not None:
        rendered = inject_portable_feedback_bootstrap(
            rendered, page_portable_feedback_script(portable_feedback))
    return rendered


def _render_link_list(
    items: list[tuple],
    bundle: Bundle,
    *,
    kind: str,
    comment_summary: CommentSummaryV1 | None = None,
) -> str:
    """Render (target_cid, label, mode, source_cid, target_raw, origin) rows.

    Legacy five-field renderer callers retain target-prefix discrimination;
    model-backed callers use origin, including OKF source derivations.

    Returns just the inner HTML (caller wraps in <section>).

    Deduplicates by target concept id (so a concept reached via BOTH a body
    link AND a typed relation shows once, with the relation type as a chip).

    P2-68: returns an EMPTY string when there are no items so the caller
    (:func:`_render_concept_page`) can strip the now-empty wrapping
    ``<section class="okf-relations__block">`` rather than rendering the
    heading + "(none)" placeholder.
    """
    if not items:
        return ""
    out = ['<ul class="okf-linklist">']
    seen_targets: dict[ConceptId, str | None] = {}  # target_cid → relation_label
    for item in items:
        target_cid, label, mode, source_cid, target_raw = item[:5]
        origin = item[5] if len(item) > 5 else None
        if target_cid is None:
            out.append(f'<li><span class="okf-muted">{_esc(label)}</span></li>')
            continue
        # Dedup by target_cid.
        is_typed_relation = origin in ("relations", "sources") if origin is not None else _is_typed_relation_target_raw(target_raw)
        if target_cid in seen_targets:
            # If we already have this target but this entry carries a typed
            # relation label, record it for display.
            if is_typed_relation and _is_relation_label(label):
                seen_targets[target_cid] = label
            continue
        seen_targets[target_cid] = (
            label if (is_typed_relation and _is_relation_label(label)) else None
        )

    # iter3 P2-2: sort seen_targets by concept_id so the rendered
    # backlinks/outlinks list is deterministic (§3.2), not relying on
    # items[] insertion order (which mirrors graph.edges order, masked
    # by Bundle.load's sorted rglob for disk-loaded bundles).
    # Phase 2: connection CARDS — each target renders with its type icon,
    # a type-tinted accent, title + id, and the typed-relation chip with a
    # direction arrow. The palette is resolved once per call (cached
    # override read; cheap at page-render granularity).
    try:
        palette = resolve_palette(bundle)
    except Exception:  # noqa: BLE001 — cards degrade to accentless
        palette = {}
    arrow = "→" if kind == "out" else "←"
    comment_counts = comment_summary.get("counts", {}) if comment_summary else None
    for target_cid, rel_label in sorted(seen_targets.items()):
        concept = bundle.concepts.get(target_cid)
        title = concept.title if concept else concept_id_to_str(target_cid)
        ctype = concept.type if concept else None
        color = palette.get(ctype or "", type_color_for(ctype))
        icon = type_icon_svg(ctype, size=16, cls="okf-type-icon")
        url = url_for_concept(target_cid, mode, source_cid=items[0][3])
        # P2-57: emit ``okf-rel-type`` (the chip class defined in wiki.css)
        # rather than the orphaned ``okf-rel-kind`` which had no CSS rule
        # and rendered typed-relation labels invisible in link lists.
        chip = (
            f'<span class="okf-rel-type">{arrow} {_esc(rel_label)}</span>'
            if rel_label else ""
        )
        cid_text = concept_id_to_str(target_cid)
        comment_chip = ""
        if comment_counts is not None and comment_counts.get(cid_text, 0) > 0:
            count = comment_counts.get(cid_text, 0)
            label_text = f'{count} active comment{"" if count == 1 else "s"}'
            comment_chip = (
                f'<a class="okf-card__comments okf-comment-count" href="{_esc(url)}" '
                f'data-concept-id="{_esc_attr_qs(cid_text)}" '
                f'aria-label="{_esc_attr_qs(label_text + " on " + title)}">{_esc(label_text)}</a>'
            )
        out.append(
            f'<li class="okf-conn" data-concept-id="{_esc_attr_qs(cid_text)}" style="--okf-type-accent:{_esc(color)}">'
            f'<span class="okf-conn__icon" style="color:{_esc(color)}" aria-hidden="true">{icon}</span>'
            f'<span class="okf-conn__body">'
            f'<a href="{_esc(url)}" class="okf-internal okf-conn__title">{_esc(title)}</a>'
            f' <span class="okf-muted okf-conn__id">{_esc(concept_id_to_str(target_cid))}</span>'
            f'</span><span class="okf-conn__meta">{chip}{comment_chip}</span></li>'
        )
    out.append("</ul>")
    return "\n".join(out)


def _is_typed_relation_target_raw(target_raw: str | None) -> bool:
    """P1-36: the canonical typed-relation discriminator.

    The graph builder (model.py:691) tags typed-relation edges with the
    magic prefix ``relation:`` on ``target_raw``. This predicate is shared
    by both render.py consumers (:func:`build_graph_data` and
    :func:`_render_link_list`) so the discriminator is consistent within
    the renderer. The clean fix lives in model.py (giving :class:`Link`
    an explicit ``origin`` field); that is owned by another bundle.
    """
    return bool(target_raw) and target_raw.startswith("relation:")


def _is_relation_label(label: str | None) -> bool:
    """P1-36: helper that filters out labels that look like paths/URLs.

    Even when ``_is_typed_relation_target_raw`` is True, the label might
    be empty or accidentally path-like; this guard ensures we only emit a
    chip when the label is plausibly a relation-type word.
    """
    return bool(label) and not label.startswith(("/", "http", "mailto"))


# P2-68: regex to strip empty ``<section class="okf-relations__block">``
# blocks after substitution. Matches a section whose only content is
# whitespace + an optional ``<h2>...</h2>`` heading. The template wraps
# both ``__OUTGOING_HTML__`` and ``__BACKLINKS_HTML__`` in such sections;
# when the link list is empty, the section collapses to ``<section ...><h2
# ...>...</h2>\n</section>`` which this regex removes.
_EMPTY_RELATIONS_SECTION_RE = re.compile(
    r'<section class="okf-relations__block"[^>]*>\s*'
    r'(?:<h2>[^<]*</h2>\s*)?'
    r'</section>\s*',
    re.DOTALL,
)


def _fmt_fm_value(v: Any) -> str:
    """Format a frontmatter value for display without leaking Python repr."""
    if isinstance(v, (list, tuple)):
        # If any item is a dict, use JSON for clean structured display
        if any(isinstance(x, dict) for x in v):
            return json.dumps(v, default=str, ensure_ascii=False, indent=2)
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, default=str, ensure_ascii=False, indent=2)
    return str(v)


# P1-39: detect frontmatter ``citations:`` so the renderer can suppress
# the duplicate body ``# Citations`` heading.
def _has_frontmatter_citations(concept: Concept) -> bool:
    """True when the concept has a non-empty ``citations:`` frontmatter key."""
    cits = concept.frontmatter.get("citations")
    return isinstance(cits, list) and bool(cits)


def _strip_body_citations_section(body_html: str) -> str:
    """P1-39: remove the body's ``# Citations`` section from rendered HTML.

    The frontmatter ``citations:`` governed key is rendered separately by
    :func:`_render_governed_keys`. When BOTH are present (a known producer
    pattern), the citations list appears twice on the rendered page. This
    helper strips the body section so only the governed-keys block remains.

    The regex matches the ``<hN...>Citations</hN>`` heading (allowing
    arbitrary attributes between the tag name and ``>``, since the markdown
    renderer emits ``<h2 id="citations">``) + all body content up to (but
    not including) the next heading. If no match is found, the body is
    returned unchanged.
    """
    pat = re.compile(
        r"<h([1-6])(?:[^>]*)>\s*Citations\s*</h\1>\s*(.*?)(?=<h[1-6][>\s]|$)",
        re.DOTALL | re.IGNORECASE,
    )
    return pat.sub("", body_html, count=1)


def _render_subtitle(concept: Concept) -> str:
    """P2-22: render the concept's aliases as a subtitle line.

    SPEC §7.1 wording: aliases are "Consumed by §4.3 entity search and the
    viewer (show as subtitles)". This emits a single ``<p>`` subtitle line
    (joined with " · ") immediately after the page title.

    P2-8 (iter-3): this subtitle is the SINGLE canonical surface for
    aliases. The previous "Also known as:" pills block in
    :func:`_render_governed_keys` was removed because it duplicated the
    same alias values within ~80px of this subtitle. Keeping one surface
    (the subtitle) gives screen-reader / SEO context AND avoids the
    information-architecture ambiguity of two renderings.

    P2-7 (iter-1): the inline ``style=`` fallback was removed; ``.okf-subtitle``
    is now a real rule in wiki.css (margin/font-size/colour via tokens).
    """
    strs = alias_labels(concept.frontmatter.get("aliases"))
    if not strs:
        return ""
    joined = " · ".join(_esc(a) for a in strs)
    return f'<p class="okf-subtitle">{joined}</p>'


def _render_breadcrumb(concept: Concept, *, mode: str, name: str) -> str:
    """P2-3 (iter-1): render a clickable breadcrumb trail.

    Emits ``{bundle_name} / tables / orders`` where each ancestor segment
    links to its directory index (``/dir/`` for serve/spa, relative
    ``index.html`` for static). The current concept (last segment) is plain
    text with ``aria-current="page"``.

    P2-10 (iter-3): the secondary ``<small class="okf-breadcrumb__id">``
    raw concept-id was removed. It duplicated the path segments already
    shown in the trail, AND it rendered at ~11.2px (``<small>`` ×
    ``.okf-muted``'s 14px × ``<small>`` default 0.8em) — below the 12px
    typographic floor used everywhere else in the viewer. The trail itself
    is now the only breadcrumb affordance. Developers who need the raw
    concept id can read it from the page URL or from the "All fields"
    details block.

    Determinism (§3.2): iteration is over the concept's own id segments,
    which are inherently ordered — no sorting needed.
    """
    cid = concept.id
    items: list[str] = []

    # Root crumb: bundle name → root index.
    if mode in ("serve", "spa"):
        root_href = "/"
    else:
        depth = len(cid) - 1
        root_href = ("../" * depth) + "index.html"
    items.append(f'<a href="{_esc(root_href)}" class="okf-breadcrumb__root">{_esc(name)}</a>')

    # Each full ancestor segment (except the last) is a directory index.
    for d in range(1, len(cid)):
        seg_label = _esc(str(cid[d - 1]))
        if mode in ("serve", "spa"):
            href = "/" + "/".join(str(s) for s in cid[:d]) + "/"
        else:
            ups = (len(cid) - 1) - d
            href = ("../" * ups) + "index.html"
        items.append(f'<a href="{_esc(href)}">{seg_label}</a>')

    # Final segment: current concept (plain text, marks current page).
    items.append(
        f'<span class="okf-breadcrumb__current" aria-current="page">{_esc(str(cid[-1]))}</span>'
    )

    sep = '<span class="okf-breadcrumb__sep" aria-hidden="true">/</span>'
    trail = f" {sep} ".join(items)
    return (
        f'<nav class="okf-breadcrumb" aria-label="Breadcrumb">'
        f'{trail}'
        f"</nav>"
    )


def _render_governed_keys(
    concept: Concept, bundle: Bundle, *, mode: str = "serve", root_prefix: str = "",
) -> str:
    """Render governed keys (aliases, entities, provenance, citations, relations).

    P2-6 (iter-1): each section now has distinct treatment sized to its
    semantic weight (the prior flat inline-run layout was too dense to scan):

      * aliases    → NOT rendered here (P2-8 iter-3): aliases are rendered
                     ONCE as the page subtitle by :func:`_render_subtitle`
                     (SPEC §7.1 wording "show as subtitles"). The prior
                     "Also known as:" pills block duplicated the subtitle
                     within ~80px of vertical space.
      * entities   → header chips via :func:`_render_entity_chips`
      * provenance → stacked list: source link / muted note / ``<time>``
      * citations  → numbered ``<ol>`` (the ``[id]`` becomes the list marker)
      * relations  → scannable typed rows (Phase 2): one ``<li>`` per relation
                     carrying the type badge, a direction arrow, a LINKED
                     target (resolved via ``bundle`` so the target's type
                     identity/icon/accent and URL are real), and an optional
                     explanation. This replaces the prior comma-joined prose
                     (``[type] → target, [type] → target``) which was too
                     dense to scan and never linked its targets.

    URLs in provenance/citations are sanitized via :func:`_safe_url`.
    ``bundle`` is required so relation targets can resolve to a real concept
    (title/type/colour/URL); an unresolvable target degrades to a muted
    plain-text span rather than a broken link.
    """
    parts: list[str] = []

    # P2-8 (iter-3): aliases are NO LONGER rendered here. The subtitle
    # paragraph from :func:`_render_subtitle` is the single canonical
    # surface for aliases (it carries the SPEC §7.1 wording and gives
    # screen-reader / SEO context). The previous "Also known as:" pills
    # block duplicated the same alias values within ~80px of the subtitle,
    # which became conspicuous once P1-1 (iter-2) moved the subtitle out
    # of the h1 as a sibling. Removing the pills keeps the alias count at
    # exactly one render per concept.

    # Phase 2: entities are NO LONGER rendered here either. They now render
    # as clickable header chips via :func:`_render_entity_chips` (ui-overhaul-
    # plan §3.3: "entity chips (pill + icon, clickable → search)"). The
    # prior <dl> duplicated the header chips and buried entity search behind
    # a definition list. Removing it keeps entities at exactly one render
    # per concept, in the scannable header band where they belong.

    # Provenance — stacked list (P2-6): source link / note / time
    provenance = concept.frontmatter.get("provenance")
    if provenance and isinstance(provenance, list):
        prov_items: list[str] = []
        for prov in provenance:
            if not isinstance(prov, dict):
                continue
            source = prov.get("source", "")
            note = prov.get("note", "")
            ts = prov.get("timestamp", "")
            line_bits: list[str] = []
            if source:
                from .viewer.markdown import _safe_url
                safe = _safe_url(str(source))
                if safe:
                    # Same static-build mapping as the resource chip: an
                    # internal /a/b.md source must point at its built page.
                    static_href = _static_md_href(safe, root_prefix)
                    if mode == "static" and static_href is not None:
                        safe = static_href
                    line_bits.append(f'<a href="{_esc(safe)}" rel="noopener">{_esc(str(source))}</a>')
                else:
                    line_bits.append(f'<span class="okf-provenance-source">{_esc(str(source))}</span>')
            if note:
                line_bits.append(f'<span class="okf-provenance-note">{_esc(str(note))}</span>')
            if ts:
                line_bits.append(f'<time class="okf-provenance-time">{_esc(str(ts))}</time>')
            if line_bits:
                prov_items.append('<li class="okf-provenance-item">' + "".join(line_bits) + "</li>")
        if prov_items:
            parts.append(
                f'<div class="okf-governed okf-governed--block okf-provenance">'
                f'<span class="okf-governed-label">Sources:</span>'
                f'<ul class="okf-provenance-list">' + "".join(prov_items) + '</ul></div>'
            )

    # Citations — numbered list (P2-6): [id] text url
    citations = concept.frontmatter.get("citations")
    if citations and isinstance(citations, list):
        cit_items: list[str] = []
        for cit in citations:
            if not isinstance(cit, dict):
                continue
            text = cit.get("text", "")
            url = cit.get("url", "")
            cid_val = cit.get("id", "")
            bits: list[str] = []
            if cid_val:
                bits.append(f'<span class="okf-cite-id">[{_esc(str(cid_val))}]</span>')
            if text:
                bits.append(f'<span class="okf-cite-text">{_esc(str(text))}</span>')
            if url:
                from .viewer.markdown import _safe_url
                safe = _safe_url(str(url))
                if safe:
                    bits.append(f'<a href="{_esc(safe)}" rel="noopener">{_esc(str(url))}</a>')
            if bits:
                cit_items.append('<li class="okf-citation">' + " ".join(bits) + "</li>")
        if cit_items:
            parts.append(
                f'<div class="okf-governed okf-governed--block okf-citations">'
                f'<span class="okf-governed-label">Citations:</span>'
                f'<ol class="okf-citation-list">' + "".join(cit_items) + '</ol></div>'
            )

    # Relations — typed-relation rows (Phase 2 scanability). Each relation is
    # a repeatable semantic list row: [type badge] → [linked target + id]
    # (+ optional explanation). The target is resolved through ``bundle`` so
    # it carries the target's real type identity (icon + accent) and a real
    # URL; an unresolvable target degrades to a muted span (no broken link).
    # This replaces the prior comma-joined prose
    # (``[type] → target, [type] → target``) which buried typed edges in a
    # dense punctuation run and never linked its targets. The wrapper keeps
    # the ``okf-relations-fm`` class and the ``okf-rel-type`` badge so
    # existing token/style consumers continue to resolve.
    relations = concept.frontmatter.get("relations")
    if relations and isinstance(relations, list):
        from .paths import concept_id_from_str as _cid_from_str, ConceptIdError as _CidErr
        try:
            palette = resolve_palette(bundle)
        except Exception:  # noqa: BLE001 — rows degrade to accentless
            palette = {}
        rel_rows: list[str] = []
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            rel_type = str(rel.get("type", "related")) or "related"
            target_raw = str(rel.get("target", "") or "").strip()
            rel_detail = rel.get("detail", "")
            if not target_raw:
                continue
            # Resolve the target concept (best-effort; degrade gracefully).
            target_concept: Concept | None = None
            try:
                target_cid = _cid_from_str(target_raw)
            except (_CidErr, ValueError):
                target_cid = None
            if target_cid is not None:
                target_concept = bundle.concepts.get(target_cid)
            if target_concept is not None:
                ttitle = target_concept.title or concept_id_to_str(target_concept.id)
                tid = concept_id_to_str(target_concept.id)
                ttype = target_concept.type
                turl = url_for_concept(target_concept.id, mode, source_cid=concept.id)
                tcolor = palette.get(ttype or "", type_color_for(ttype))
                ticon = type_icon_svg(ttype, size=14, cls="okf-type-icon")
                target_html = (
                    f'<a class="okf-relation__target" href="{_esc(turl)}" '
                    f'style="--okf-type-accent:{_esc(tcolor)}">'
                    f'<span class="okf-relation__icon" style="color:{_esc(tcolor)}" '
                    f'aria-hidden="true">{ticon}</span>'
                    f'<span class="okf-relation__title">{_esc(ttitle)}</span>'
                    f'<span class="okf-muted okf-relation__id">{_esc(tid)}</span>'
                    f'</a>'
                )
            else:
                # Unresolvable target: muted plain text, no actionable link.
                target_html = (
                    f'<span class="okf-relation__target okf-relation__target--unresolved">'
                    f'<span class="okf-relation__title">{_esc(target_raw)}</span>'
                    f'</span>'
                )
            detail_html = (
                f'<span class="okf-rel-detail">{_esc(str(rel_detail))}</span>'
                if rel_detail else ""
            )
            rel_rows.append(
                f'<li class="okf-relation-row">'
                f'<span class="okf-rel-type">{_esc(rel_type)}</span>'
                f'<span class="okf-relation__arrow" aria-hidden="true">→</span>'
                f'{target_html}{detail_html}'
                f'</li>'
            )
        if rel_rows:
            parts.append(
                f'<div class="okf-governed okf-governed--block okf-relations-fm">'
                f'<span class="okf-governed-label">Relations:</span>'
                f'<ul class="okf-relations-list">' + "".join(rel_rows) + '</ul></div>'
            )

    return "\n".join(parts)


# ---- Phase 2: concept anatomy (infobox / entity chips / prev-next) -------


def _render_entity_chips(
    concept: Concept, *, mode: str = "serve", root_prefix: str = "",
) -> str:
    """Render frontmatter entities as clickable chips in the header band.

    Each chip links to the search page with the entity label as the query,
    so a reader can find every concept mentioning the same entity. Both
    plain-string entities and dict entities (``label``/``kind``) are
    supported.

    Security: the label is escaped for both visible text (``_esc``) and the
    query-string href (``_esc_attr_qs``). Untrusted ``kind`` strings are
    escaped too. The search target is built from the same mode-aware
    pattern as ``_nav_controls_html``.
    """
    entities = concept.frontmatter.get("entities")
    if not entities or not isinstance(entities, list):
        return ""
    search_target = f"{root_prefix}__search{'.html' if mode == 'static' else ''}"
    chips: list[str] = []
    for ent in entities:
        if isinstance(ent, str):
            label = ent.strip()
            kind: Any = None
        elif isinstance(ent, dict):
            label = str(ent.get("label", "") or "").strip()
            kind = ent.get("kind")
        else:
            continue
        if not label:
            continue
        # URL-encode the query value (spaces → +) then HTML-escape the
        # full href for safe attribute interpolation.
        qs = urlencode({"q": label})
        href = _esc_attr_qs(f"{search_target}?{qs}")
        kind_html = (
            f'<span class="okf-entity-chip__kind">{_esc(str(kind))}</span>'
            if kind else ""
        )
        chips.append(
            f'<a class="okf-entity-chip" href="{href}">'
            f'<span class="okf-entity-chip__label">{_esc(label)}</span>{kind_html}'
            f'</a>'
        )
    if not chips:
        return ""
    return (
        '<div class="okf-entity-chips" aria-label="Entities">'
        + "".join(chips) + '</div>'
    )


def _section_index_entry_order(
    bundle: Bundle, section: ConceptId
) -> list[ConceptId] | None:
    """Resolve the authoritative sibling order for a section from its
    hand-authored ``index.md`` (SPEC §6).

    The directory index at ``<section>/index.md`` declares the section's
    intended reading order via its ``* [label](target)`` entries IN DOCUMENT
    ORDER. That order drives the prev/next-in-section footer navigation —
    the author curated it, so it is authoritative over any
    lexical / source-path sort.

    Each target is resolved against the section prefix (``orders`` in
    ``tables/index.md`` → ``("tables", "orders")``); targets that escape the
    section (``../glossary``) or do not correspond to a real concept are
    skipped. Returns ``None`` when no section index exists or it declares no
    resolvable same-section siblings — callers then fall back to source order.
    """
    if not section:
        return None
    rel = Path(*[p for p in section]) / "index.md"
    index_file = bundle.indexes.get(rel)
    if index_file is None:
        return None
    section_prefix = "/".join(section)
    seen: set[ConceptId] = set()
    order: list[ConceptId] = []
    for _label, target, _desc in index_file.entries():
        if not target:
            continue
        # Targets are kept verbatim (IndexFile.entries docstring). Strip any
        # leading ./ or ../ and a trailing .md so the common authored forms
        # (./orders.md, orders, orders.md) all resolve.
        t = target.strip()
        # Reject cross-directory references up-front: a sibling nav must stay
        # inside the current section. ../anything escapes the section.
        if t.startswith("../") or t.startswith("/") or "/../" in t:
            continue
        if t.startswith("./"):
            t = t[2:]
        # Bare fragment / external / mailto — not a same-section sibling.
        if not t or t.startswith("#") or ":" in t:
            continue
        try:
            cid = concept_id_from_str(section_prefix + "/" + t) if section else concept_id_from_str(t)
        except Exception:  # noqa: BLE001 — verbatim targets can be malformed
            continue
        # Only same-section ids that point at real concepts are siblings.
        if len(cid) != len(section) + 1 or cid[:-1] != section:
            continue
        if cid not in bundle.concepts:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        order.append(cid)
    return order or None


def _render_prevnext(concept: Concept, bundle: Bundle, *, mode: str) -> str:
    """Deterministic prev/next sibling navigation within the same directory
    section, ordered by the AUTHORITATIVE section ``index.md`` when the author declared one.

    Order precedence:
      1. Section ``index.md`` entries in DOCUMENT ORDER — the author curated
         this list, so it is the source of truth (the plan calls this "the
         index ordering"). A hand-authored ``tables/index.md`` listing
         ``[Beta](beta)`` before ``[Alpha](alpha)`` makes Beta precede
         Alpha in prev/next, even though lexical cid sort would invert them.
      2. Source order (``bundle.concepts`` iteration = path-sorted load
         order) when no section index declares an order. This is stable and
         reproducible across builds, and matches the order readers see in
         the directory listing.

    The prior implementation sorted siblings by concept id, which silently
    overrode the author's curated index order whenever the two disagreed.
    Returns ``""`` when the concept is top-level (no section) or has no
    siblings. Both ``rel="prev"`` / ``rel="next"`` link types are emitted so
    browsers and assistive tech can surface the sequential reading order.
    A placeholder span keeps the grid balanced when only one neighbour exists.
    """
    cid = concept.id
    if len(cid) < 2:
        return ""
    section = cid[:-1]
    # Source-order siblings (path-sorted load order). Always deterministic;
    # used directly when no section index declares an order.
    source_siblings = [
        c for c in bundle.concepts.values() if c.id[:-1] == section
    ]
    if len(source_siblings) <= 1:
        return ""

    authoritative_order = _section_index_entry_order(bundle, section)
    if authoritative_order is not None:
        # Build the ordered sibling list from the index entries; any sibling
        # NOT listed in the index is appended at the end in source order so
        # a stale index (missing a newly-added concept) does not strand it
        # without prev/next.
        listed = set(authoritative_order)
        ordered_ids = list(authoritative_order)
        for c in source_siblings:
            if c.id not in listed:
                ordered_ids.append(c.id)
        siblings = [bundle.concepts[i] for i in ordered_ids if i in bundle.concepts]
    else:
        siblings = source_siblings

    cur_idx: int | None = None
    for i, c in enumerate(siblings):
        if c.id == cid:
            cur_idx = i
            break
    if cur_idx is None:
        return ""
    prev_c = siblings[cur_idx - 1] if cur_idx > 0 else None
    next_c = siblings[cur_idx + 1] if cur_idx < len(siblings) - 1 else None
    if not prev_c and not next_c:
        return ""

    def _nav_item(c: Concept, label: str, rel: str) -> str:
        url = url_for_concept(c.id, mode, source_cid=cid)
        return (
            f'<a class="okf-prevnext__link okf-prevnext__{rel}" href="{_esc(url)}" '
            f'rel="{rel}">'
            f'<span class="okf-prevnext__label">{label}</span>'
            f'<span class="okf-prevnext__title">{_esc(c.title)}</span>'
            f'</a>'
        )

    parts = ['<nav class="okf-prevnext" aria-label="Section navigation">']
    parts.append(
        _nav_item(prev_c, "Previous", "prev") if prev_c
        else '<span class="okf-prevnext__link okf-prevnext__prev okf-prevnext__placeholder" aria-hidden="true"></span>'
    )
    parts.append(
        _nav_item(next_c, "Next", "next") if next_c
        else '<span class="okf-prevnext__link okf-prevnext__next okf-prevnext__placeholder" aria-hidden="true"></span>'
    )
    parts.append('</nav>')
    return "".join(parts)


def _render_connections(outgoing_html: str, backlinks_html: str) -> str:
    """Wrap outgoing/incoming link cards in a connections footer.

    Each non-empty list becomes a labelled card row. Empty lists produce
    no block at all (so a concept with no backlinks shows only "This
    concept links to"). The card markup itself comes from
    :func:`_render_link_list` (unchanged — it already emits type-icon +
    tinted-accent + title + relation-chip cards).
    """
    blocks: list[str] = []
    if outgoing_html:
        blocks.append(
            '<section class="okf-connections__block" id="okf-outgoing">'
            '<h2 class="okf-connections__heading">This concept links to</h2>'
            + outgoing_html + '</section>'
        )
    if backlinks_html:
        blocks.append(
            '<section class="okf-connections__block" id="okf-backlinks">'
            '<h2 class="okf-connections__heading">Referenced by</h2>'
            + backlinks_html + '</section>'
        )
    if not blocks:
        return ""
    count = outgoing_html.count("<li") + backlinks_html.count("<li")
    nav = (
        '<nav class="okf-page__connections" aria-label="Concept connections">'
        + "".join(blocks) + '</nav>'
    )
    # Long inventories alone earn disclosure: short recommendation lists stay
    # immediately visible, while large lists retain every link behind a count.
    if count > 8:
        return (
            '<details class="okf-connections-disclosure">'
            f'<summary>Connections ({count})</summary>{nav}</details>'
        )
    return nav


def _render_onthispage(body_html: str) -> str:
    """Render the shared near-header 'On this page' disclosure.

    Emits a ``<details class="okf-onthispage">`` from the body's rendered
    heading IDs and text. It provides native no-JavaScript anchors and the
    stable host enhanced by ``document-outline.js`` across supported concept
    surfaces.

    The shared ``document-outline.js`` owner keeps the disclosure in sync
    after live body patches (it listens for ``okf-loom:bodyPatched`` and
    rebuilds from the live DOM).

    Implementation note: the list is parsed from the RENDERED body HTML, not
    from ``concept.headings``. The renderer's slugify
    (:func:`okf_loom.viewer.markdown._slugify`) and the parser's
    (:func:`okf_loom.parse._slugify`) differ on underscores and the empty
    fallback, so ``concept.headings`` slugs can diverge from the ids actually
    emitted on the ``<hN>`` elements. Reading the rendered ids is the only
    authoritative source, and it works identically in serve, spa, and static
    modes (and with JS off, since the links resolve to in-page anchors).

    F8: the host is emitted for HEADINGLESS bodies too, as a stable
    ``hidden`` + ``data-okf-outline-empty`` shell with no list. The client
    owner keys its whole lifecycle on this slot, so omitting it used to
    disarm the script: a headingless document that gained headings through a
    live mutation never grew an outline without a reload. With the host
    always present, the owner rebuilds on the patch and un-hides the slot;
    with JavaScript off the empty shell stays hidden (no disclosure chrome).
    """
    items: list[tuple[int, str, str]] = []
    for m in _ONTHISPAGE_HEADING_RE.finditer(body_html):
        level = int(m.group(1))
        hid = m.group(2)
        # The body_html fragment carries HTML-ESCAPED text (e.g. an "&" in the
        # heading becomes "API &amp; SDK") plus inline markup (<a>/<code>).
        # The disclosure is a plain-text jump list, not a re-render, so:
        #   1. strip tags (inline links/code → their text),
        #   2. DECODE entities to canonical visible text (so the final _esc()
        #      below encodes exactly once), and
        #   3. collapse whitespace.
        # Without the unescape, _esc re-encodes the already-escaped fragment —
        # "API &amp; SDK" became "API &amp;amp; SDK" (double-escape, rendering
        # as the literal "API &amp; SDK"). Decoding first makes the SSR label
        # byte-for-byte equal to the live rebuild in document-outline.js,
        # which reads rendered heading text (already decoded by the browser)
        # and normalizes whitespace the same way. The decoded
        # text is only ever emitted through _esc(), so it stays safe text.
        text = re.sub(r"<[^>]+>", "", m.group(3))
        text = _html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        items.append((level, hid, text))
    if not items:
        return (
            '<details class="okf-onthispage" aria-label="On this page" '
            'data-okf-document-outline data-okf-outline-empty hidden>'
            '<summary><span>On this page</span>'
            '<span class="okf-onthispage__current" aria-hidden="true"></span></summary>'
            '</details>'
        )
    min_level = min(l for l, _, _ in items)
    out = ['<details class="okf-onthispage" aria-label="On this page" data-okf-document-outline>']
    out.append('<summary><span>On this page</span><span class="okf-onthispage__current" aria-hidden="true"></span></summary>')
    out.append('<nav class="okf-onthispage__nav" aria-label="Document sections"><ol class="okf-onthispage__list">')
    for level, hid, text in items:
        depth = max(0, level - min_level)
        cls = "okf-onthispage__item" + (f" okf-onthispage--l{depth}" if depth else "")
        out.append(
            f'<li class="{cls}"><a class="okf-onthispage__link" href="#{_esc(hid)}">'
            f'{_esc(text)}</a></li>'
        )
    out.append('</ol></nav></details>')
    return "".join(out)


# Headings as emitted by the markdown renderer: ``<hN id="slug"[ data-...]>inline</hN>``.
# id may appear after a data-owner attribute, so the regex allows attributes
# before/after the id. Non-greedy + DOTALL so multi-line heading content matches.
_ONTHISPAGE_HEADING_RE = re.compile(
    r'<h([1-6])[^>]*?\bid="([^"]+)"[^>]*>(.*?)</h\1>',
    re.DOTALL,
)


def _render_infobox(
    concept: Concept,
    *,
    type_color: str,
    revision: str,
    tags_html: str,
    frontmatter_html: str,
    governed_html: str,
    long_document: bool = False,
) -> str:
    """Render the Document details disclosure (structured facts).

    Document details is emitted after the authored reading region, outside
    the projected body, so annotation/projection parity is unaffected. It is
    a native ``<details>`` whose summary is the Document details label plus
    the compact type/rev row, rendered WITHOUT the ``open`` attribute:
    collapsed by default at every width. Readers own its open state;
    viewport changes do not open it, and with JS off the native toggle
    remains fully usable. Additional fields
    retain their nested native disclosure (``details.okf-infobox__facts``,
    "All fields").

    Governance and additional-field sections are omitted when empty;
    About/Classification retain explicit labels even when their values are
    sparse so the disclosure anatomy stays stable across concepts.

    Type tint (layout-graph / contract-runtime parity): ``--okf-type-accent``
    is set on the ``<details>`` itself (not only on the inline type label) so
    the infobox's tinted left border and the governed relation rows resolve
    to the concept's actual type colour. The header band sets the same var
    on ``.okf-page__header``; because the infobox is a sibling (not a
    descendant) it must carry its own declaration or it would silently fall
    back to the generic ``--okf-accent``.
    """
    icon = type_icon_svg(concept.type, size=16, cls="okf-type-icon")
    rev_short = revision[:8] if revision else ""
    summary = (
        f'<summary class="okf-infobox__summary">'
        # The type NAME is text in the theme ink (wiki.css); the type colour
        # stays on the icon through the infobox's --okf-type-accent. Type
        # colours are mid-tone fills: as text they fell to 2.3:1.
        f'<span class="okf-infobox__type">'
        f'{icon} {_esc(concept.type or "concept")}</span>'
        f'<span class="okf-infobox__rev" title="Content revision {_esc(revision)}">'
        f'rev\u00a0{_esc(rev_short)}</span>'
        f'</summary>'
    )
    body_parts: list[str] = []
    body_parts.append(
        '<section class="okf-infobox__section okf-infobox__section--about" role="group" aria-labelledby="okf-infobox-about-title">'
        '<h2 id="okf-infobox-about-title">About</h2>'
        f'<dl class="okf-infobox__about"><dt>Content revision</dt>'
        f'<dd><code>{_esc(rev_short or "not available")}</code></dd></dl>'
        '</section>'
    )
    classification_tags_html = tags_html or '<span class="okf-muted">No tags recorded.</span>'
    body_parts.append(
        '<section class="okf-infobox__section okf-infobox__section--classification" role="group" aria-labelledby="okf-infobox-classification-title">'
        '<h2 id="okf-infobox-classification-title">Classification</h2>'
        f'<div class="okf-infobox__tags">{classification_tags_html}</div>'
        '</section>'
    )
    # The frontmatter facts table is the former "All fields" <details>.
    # Only emit it when there are genuine extra/timestamp keys (the inline
    # builder produces ``<p class='okf-muted'>(none)</p>`` for empty fm, and
    # a bare ``<table><tbody></tbody></table>`` when all keys were governed
    # or header-rendered). Checking for a <tr> is robust against both.
    if frontmatter_html and "<tr>" in frontmatter_html:
        body_parts.append(
            '<section class="okf-infobox__section okf-infobox__section--additional" role="group" aria-labelledby="okf-infobox-additional-title">'
            '<h2 id="okf-infobox-additional-title">Additional fields</h2>'
            '<details class="okf-infobox__facts">'
            '<summary>All fields</summary>'
            + frontmatter_html + '</details></section>'
        )
    if governed_html.strip():
        body_parts.append(
            '<section class="okf-infobox__section okf-infobox__section--governance" role="group" aria-labelledby="okf-infobox-governance-title">'
            '<h2 id="okf-infobox-governance-title">Governance</h2>'
            f'<div class="okf-infobox__governed">{governed_html}</div>'
            '</section>'
        )
    body = f'<div class="okf-infobox__body">{"".join(body_parts)}</div>'
    # data-okf-type-icon exposes the resolved icon key (the same key the
    # search backend ships) so wiki.js's recent-page record can store the
    # icon alongside type/type_color and the live-suggest chip can render
    # the SAME icon for recent pages as for fresh search results — without
    # a second client-side key resolver.
    icon_key = type_icon_key(concept.type)
    return (
        '<details class="okf-page__infobox" id="okf-document-details" aria-labelledby="okf-infobox-label" '
        f'data-okf-long-document="{("true" if long_document else "false")}" data-okf-heading-threshold="8" '
        f'data-okf-type-icon="{_esc(icon_key)}" '
        f'style="--okf-type-accent:{_esc(type_color)}">'
        + summary.replace(
            '<span class="okf-infobox__type"',
            '<span class="okf-infobox__label" id="okf-infobox-label">Document details</span>'
            '<span class="okf-infobox__type"',
            1,
        ) + body + '</details>'
    )


# A compact check-mark glyph for the v2 spotlight feature list (decorative:
# aria-hidden; the adjacent <span> text is the accessible name).
_V2_CHECK_SVG = (
    '<svg class="okf-v2-spotlight__check" width="14" height="14" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
    'focusable="false"><path d="M20 6L9 17l-5-5"/></svg>'
)

# Scannable v2 capability labels (4–6 items, per the prominence/balance brief).
_V2_SPOTLIGHT_FEATURES = (
    "Anchor health",
    "Explicit re-anchor",
    "Six-mode search",
    "Deterministic Context Packs",
    "Live comments + undo",
)


def _v2_spotlight_html(bundle: Bundle, mode: str, graph_href: str) -> str:
    """The 'New in Annotations v2' hero spotlight module (root index only).

    Prominence/balance P1: the v2 hands-on demo becomes the PRIMARY above-the-
    fold CTA and 'Open graph' its secondary, instead of the graph being
    the strongest hero button while v2 is only inline intro text. A tertiary
    'Read what changed' text link closes the user-outcome gap — a reader who
    wants the honest summary of what shipped reaches the release-notes concept
    in one click. F13: this navigating link says 'Open graph', distinct from
    the hero's inline 'Preview graph' disclosure which only reveals the
    thumbnail here.

    Fail-closed: the module is emitted ONLY when the target demo concept
    (``demo/annotations_v2_showcase``) exists in THIS bundle, so a generic
    bundle never ships a broken demo link. The tertiary release-notes link is
    emitted only when ``explanation/v2_release_notes`` also exists, so a bundle
    without that concept ships no broken tertiary link. Returns "" otherwise.
    """
    demo_cid = ("demo", "annotations_v2_showcase")
    if demo_cid not in bundle.concepts:
        return ""
    demo_url = url_for_concept(demo_cid, mode)
    graph_icon = type_icon_svg("model", size=14, cls="okf-v2-spotlight__graphicon")
    feat_items = "".join(
        f"<li>{_V2_CHECK_SVG} <span>{_esc(f)}</span></li>"
        for f in _V2_SPOTLIGHT_FEATURES
    )
    # Tertiary 'Read what changed' text link — fail-closed on the release-notes
    # concept existing in this bundle. Rendered last so the primary/secondary
    # CTAs keep first focus; styled as a quiet text link, not a filled button.
    release_cid = ("explanation", "v2_release_notes")
    tertiary = ""
    if release_cid in bundle.concepts:
        release_url = url_for_concept(release_cid, mode)
        tertiary = (
            '<a class="okf-v2-spotlight__tertiary" '
            f'href="{_esc(release_url)}">Read what changed →</a>'
        )
    return (
        '<aside class="okf-v2-spotlight" aria-labelledby="okf-v2-spotlight-title">'
        '<p class="okf-v2-spotlight__eyebrow">New</p>'
        '<h2 class="okf-v2-spotlight__title" id="okf-v2-spotlight-title">'
        "New in Annotations v2</h2>"
        '<p class="okf-v2-spotlight__outcome">'
        "Comments used to drift silently when wording changed. "
        "v2 now tracks anchor health, re-anchors explicitly, and resolves "
        "deterministically \u2014 so review stays tied to the words you wrote."
        "</p>"
        f'<ul class="okf-v2-spotlight__features">{feat_items}</ul>'
        '<div class="okf-v2-spotlight__actions">'
        '<a class="okf-btn okf-btn--primary okf-v2-spotlight__cta" '
        f'href="{_esc(demo_url)}">Try the v2 hands-on demo</a>'
        '<a class="okf-btn okf-btn--quiet okf-v2-spotlight__secondary" '
        f'href="{_esc(graph_href)}">{graph_icon} Open graph</a>'
        f"{tertiary}"
        "</div>"
        "</aside>"
    )


_RESERVED_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; "
    "connect-src 'self' https://cdn.jsdelivr.net; font-src 'self' https://cdn.jsdelivr.net; "
    "object-src 'none'; base-uri 'self'; form-action 'self'"
)


def _reserved_document_links(rendered: str, document: str, mode: str,
                             bundle: Bundle | None) -> str:
    """Translate a reserved page's links to real destinations for ``mode``.

    Resolution stays in the navigation seam (canonical ``/path.md#fragment``
    from the page's own directory). This pass only maps each canonical
    identity to the page that actually exists in the output: concept URLs for
    the mode, reserved index/log pages, native same-page fragments, and the
    shared ``data-okf-broken`` flag when the target is not in the bundle.
    """
    if bundle is None:
        return normalize_document_links(rendered, document, mode)
    concepts = {concept_id_to_str(c.id) for c in bundle.concepts.values()}
    reserved = {p.as_posix() for p in bundle.indexes} | {p.as_posix() for p in bundle.logs}
    reserved.add('index.md')  # a root index page is always emitted
    base = posixpath.dirname(document) or '.'

    def replace(match: re.Match[str]) -> str:
        attrs = match.group(1)
        found = re.search(r'\bhref="([^"]*)"', attrs)
        if found is None:
            return match.group(0)
        path, _, fragment = _html.unescape(found.group(1)).partition('#')
        if not (path.startswith('/') and path.endswith('.md')):
            return match.group(0)
        target = path[1:]
        anchor = '#' + fragment if fragment else ''
        broken = ''
        if target == document and anchor:
            dest = ''
        elif target[:-3] not in concepts and target not in reserved:
            dest, broken = path, ' data-okf-broken="1"'
        elif mode == 'static':
            dest = posixpath.relpath(target[:-3] + '.html', base)
        elif target[:-3] in concepts:
            dest = '/' + target[:-3]
        elif posixpath.basename(target) == 'index.md':
            dest = '/' + target[:-len('index.md')]
        elif mode == 'spa':
            dest = '/' + target[:-3] + '.html'
        else:
            dest = '/' + target
        # The shared Markdown renderer classifies a link before it is resolved,
        # so a fragment or sibling link can arrive marked external/new-tab. It
        # resolved inside this bundle: present it as the internal link it is.
        attrs = re.sub(r'\s(?:class="okf-(?:external|internal)"|rel="noopener noreferrer"|target="_blank")',
                       '', attrs)
        href = _html.escape(dest + anchor, quote=True)
        attrs = re.sub(r'\bhref="[^"]*"', lambda _m: f'href="{href}"', attrs, count=1)
        return f'<a{attrs} class="okf-internal"{broken}>'

    canonical = normalize_document_links(rendered, document, 'serve')
    return re.sub(r'<a\b([^>]*)>', replace, canonical)


def render_reserved_document(document: str, body: str, *, mode: str,
                             config: dict[str, Any], bundle: Bundle | None = None,
                             name: str | None = None) -> str:
    """Reserved Markdown (log) reading page in the shared application shell.

    The page is a real reading destination: language, viewport, theme, skip
    link, application shell and a ``main`` landmark, so it is usable on a
    phone and can host the live Studio feedback surface. It never fabricates a
    concept: identity is the bundle-relative ``document`` path only.
    """
    depth = document.count('/')
    serve_like = mode in {'serve', 'spa'}
    root_prefix = '/' if serve_like else '../' * depth
    prefix = '/__static' if serve_like else '../' * depth + '__static'
    suffix = '.html' if mode == 'static' else ''
    display = name or (bundle.root.name if bundle is not None else document)
    configured_theme, configured_contrast, configured_border = _theme_preference(config)
    initial_theme = _resolve_initial_theme(configured_theme)
    initial_mode = "auto" if configured_theme == "auto" else configured_theme.rsplit("-", 1)[1]
    home = '/' if serve_like else root_prefix + 'index.html'
    graph_href = f"{root_prefix}__graph{suffix}"
    shell_html = _application_shell_html(
        name=display, bundle_href=home, search_href=f"{root_prefix}__search{suffix}",
        index_href=home, graph_href=graph_href, current="index", mode=mode,
        initial_theme=initial_theme, initial_mode=initial_mode,
        initial_contrast=configured_contrast, initial_border=configured_border,
    )
    rendered = _reserved_document_links(markdown_to_html(body), document, mode, bundle)
    heading = '' if re.search(r'<h1\b', rendered) else f'<h1>{_esc(document)}</h1>'
    revision = ''
    if bundle is not None:
        from .retrieval import bundle_revision
        revision = bundle_revision(bundle)
    data_attrs = (f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}" '
                  f'data-okf-studio="{("1" if mode == "serve" else "0")}" '
                  f'data-okf-document="{_esc_attr_qs(document)}"')
    if revision:
        data_attrs += (f' data-okf-revision="{_esc_attr_qs(revision)}"'
                       f' data-okf-capture-revision="{_esc_attr_qs(revision)}"')
    head_theme_js = "" if mode == "serve" else f'<script src="{prefix}/theme.js"></script>'
    # The shared inline icon, from the BUILT-IN index template only (never a
    # bundle override), so the page does not request a missing /favicon.ico.
    icon = re.search(r'<link rel="icon"[^>]*>', load_template("index_page.html"))
    icon_html = icon.group(0) + '\n' if icon else ''
    # Exports say what they are, exactly like concept and index pages; this is
    # also where renderers.js mounts Review in Studio from the review metadata.
    static_note = '' if mode == 'serve' else (
        '<p class="okf-studio-fallback-banner okf-static-viewer-note" role="status">'
        'Static viewer: read and copy. Live authoring is unavailable. '
        f'<a href="{_esc_attr_qs(home)}">Index</a>.</p>\n')
    page = (
        f'<!DOCTYPE html>\n<html lang="en" data-theme="{initial_theme}"'
        f'{_theme_modifier_attrs(configured_contrast, configured_border)}>\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_RESERVED_CSP}">\n'
        f'<title>{_esc(document)} — {_esc(display)}</title>\n'
        + icon_html +
        f'<link rel="stylesheet" href="{prefix}/wiki.css">\n'
        f'<link rel="stylesheet" href="{prefix}/shell.css">\n'
        + _theme_config_html(configured_theme, configured_contrast, configured_border) + '\n'
        + head_theme_js + '\n</head>\n'
        f'<body class="okf-viewer okf-viewer--reserved" {data_attrs}>\n'
        '<a href="#okf-main" class="okf-skip-link">Skip to content</a>\n'
        + shell_html + '\n'
        + static_note +
        '<main id="okf-main" class="okf-reserved">\n'
        f'<article class="okf-reserved__article" data-okf-document="{_esc_attr_qs(document)}">\n'
        '<header class="okf-reserved__header">'
        f'<p class="okf-reserved__path"><span class="okf-reserved__kind">Update log</span> '
        f'<code>{_esc(document)}</code></p></header>\n'
        f'<div class="okf-prose okf-reserved__body">{heading}{rendered}</div>\n'
        '</article>\n</main>\n'
        f'<script src="{prefix}/capture-readiness.js" defer></script>\n'
        f'<script src="{prefix}/wiki.js" defer></script>\n'
        f'<script src="{prefix}/renderers.js" defer></script>\n'
        '</body>\n</html>\n'
    )
    return inject_review_metadata(page, config.get('review_base_url'), document) if mode != 'serve' else page


def _render_index_page(
    bundle: Bundle,
    *,
    mode: str,
    name: str,
    palette: dict[str, str],
    config: dict[str, Any],
    sub: str,
    index_file,
    comment_summary: CommentSummaryV1 | None = None,
    engagement_events: list[dict[str, Any]] | None = None,
) -> str:
    template = load_template("index_page.html", bundle)
    if mode != 'serve':
        template = inject_review_metadata(template, config.get('review_base_url'), (sub + '/' if sub else '') + 'index.md')
    static_prefix = "/__static" if mode in ("serve", "spa") else (
        "../" * (len(sub.split("/")) if sub else 0) + "__static" if sub else "__static"
    )

    # Title and intro. The hero receives only the leading authored overview;
    # the remaining index body is preserved below the generated type sections
    # so a long hand-authored index cannot turn the hero into a multi-screen
    # container or delay the first browseable concept group.
    intro_remainder = ""
    title_anchor = ''
    if sub:
        title = f"{name} / {sub}"
        intro = normalize_document_links(markdown_to_html(index_file.body), sub + '/index.md', mode) if index_file else ''
    else:
        title = name
        intro_md = ""
        authored_heading = None
        root_idx = bundle.root_index()
        if root_idx and root_idx.body.strip():
            intro_md = root_idx.body
            authored_heading = re.match(r"\s*#\s+([^\n]+)\n", intro_md)
            if authored_heading:
                title = authored_heading.group(1).strip()
                from .viewer.markdown import _slugify
                title_anchor = ' id="' + _esc_attr_qs(_slugify(title)) + '"'
        intro = markdown_to_html(intro_md) if intro_md else f'<p>{len(bundle.concepts)} concepts across {len(bundle.types())} types.</p>'
        if authored_heading:
            # Transfer only the first heading to the shell title. Render the
            # whole source first so later duplicate heading IDs keep their suffix.
            intro = re.sub(r'^\s*<h1\b[^>]*>.*?</h1>', '', intro, count=1, flags=re.DOTALL)
        # iter3 P1-1: demote headings in the root-index intro body so a
        # bundle whose index.md starts with "# Bundle Name" does not
        # produce a SECOND <h1> with the same text as the template's own
        # <h1 class="okf-index__title">. Concept pages already do this at
        # render.py:1062; this matches the discipline.
        if intro_md:
            intro = _demote_headings(intro)
            # The root index.md body was the ONE rendered-markdown surface
            # that skipped rewrite_internal_links (concept bodies route
            # through _render_link_map): in static builds every
            # hand-authored `demo/showcase.md`-style link 404'd because
            # only .html files exist in the output.
            intro = rewrite_internal_links(
                intro, _index_body_link_map(bundle, intro_md, mode)
            )
            if mode == "static":
                # Root index sits at the bundle root: "./" is its root prefix.
                intro = _relativize_asset_srcs(intro, "./")
            first_paragraph = re.search(
                r"<p(?:\s[^>]*)?>.*?</p>", intro, re.DOTALL | re.IGNORECASE,
            )
            if first_paragraph:
                excerpt_end = first_paragraph.end()
                intro, intro_remainder = intro[:excerpt_end], intro[excerpt_end:]

    # Hero v2 spotlight (root index only): promotes the v2 hands-on demo to the
    # PRIMARY above-the-fold CTA and demotes 'Open graph' to secondary. The
    # hero's <details> below is a DIFFERENT action (F13): 'Preview graph'
    # reveals the inline thumbnail without leaving the page, while the
    # spotlight secondary navigates to /__graph — distinct labels, both kept.
    # Fail-closed — emitted only when the demo concept exists in this bundle, so
    # a generic bundle never ships a broken demo link. graph_href matches mode.
    graph_href_root = "/__graph" if mode in ("serve", "spa") else "__graph.html"
    graph_thumbnail = ""
    if not sub:
        graph_thumbnail = ('<details class="okf-home-graph"><summary>Preview graph</summary>'
                           + _graph_thumbnail_html(build_graph_data(bundle, name=name), graph_href_root)
                           + '</details>')
    hero_v2 = _v2_spotlight_html(bundle, mode, graph_href_root) if not sub else ""
    hero_modifier = " okf-hero--split" if hero_v2 else ""

    # Hero stat chips (root index only): concepts / types / links counts. When
    # the v2 spotlight is present it already carries 'Open graph' as the
    # secondary CTA, so the graph link is dropped here to avoid a duplicate /
    # competing primary-looking button. Fail-soft on graph errors.
    hero_stats = ""
    if not sub:
        n_edges = 0
        try:
            n_edges = len([e for e in bundle.graph().edges if e.target is not None])
        except Exception:  # noqa: BLE001 — stats are decorative
            n_edges = 0
        stat_chips = (
            f'<div class="okf-hero__stats">'
            f'<span class="okf-stat"><strong>{len(bundle.concepts)}</strong> concepts</span>'
            f'<span class="okf-stat"><strong>{len(bundle.types())}</strong> types</span>'
            f'<span class="okf-stat"><strong>{n_edges}</strong> links</span>'
        )
        stat_chips += "</div>"
        hero_stats = stat_chips

    # Determine which concepts to list. Subdirectory indexes list direct
    # children only (unchanged). The ROOT index lists EVERY concept in the
    # bundle grouped by type — phase 1 dashboard: most bundles nest all
    # concepts one level deep, which used to leave the root with nothing
    # but directory tiles (zero concepts above the fold).
    sub_parts = tuple(sub.split("/")) if sub else ()
    direct: list[Concept] = []
    for c in bundle.concepts.values():
        if not sub_parts:
            direct.append(c)
        elif len(c.id) == len(sub_parts) + 1 and c.id[:len(sub_parts)] == sub_parts:
            direct.append(c)

    comment_counts: dict[str, int] = {}
    if (comment_summary is not None
            and comment_summary.get("schema") == 1
            and comment_summary.get("semantics") == "active-comment-row"
            and isinstance(comment_summary.get("counts"), dict)):
        comment_counts = comment_summary["counts"]

    recent_rows = _recent_engagement_rows(bundle, engagement_events) if not sub else []
    engagement_html = ""
    if recent_rows:
        recent_items: list[str] = []
        for row in recent_rows:
            href = url_for_concept(concept_id_from_str(row["id"]), mode)
            stable = re.sub(r"[^a-zA-Z0-9_-]", "-", row["event_id"] + "-" + row["id"])
            # F14: reader words + an explicit separator between the action
            # phrase and the time (the accessible name glued "disk changed"
            # to the timestamp). Raw actor/action/ts stay as attributes.
            # The separator is screen-reader-only: a bare text node became an
            # anonymous grid item in the row's two-column grid (a stray dot in
            # column 2, the time wrapped to a second line). studio.js emits
            # the identical markup for the live rail.
            recent_items.append(
                f'<li id="okf-recent-{_esc_attr_qs(stable)}" data-concept-id="{_esc_attr_qs(row["id"])}" '
                f'data-event-action="{_esc_attr_qs(row["action"])}" '
                f'data-event-actor="{_esc_attr_qs(row["actor"])}">'
                f'<a class="okf-recent__item" href="{_esc_attr_qs(href)}">'
                f'<span><strong>{_esc(row["title"])}</strong> — '
                f'{_esc(_recent_row_reader_text(row))}</span>'
                '<span class="sr-only"> · </span>'
                f'<time class="okf-recent__time" datetime="{_esc_attr_qs(row["ts"])}">'
                f'{_esc(_recent_time_text(row["ts"]))}</time></a></li>'
            )
        engagement_html = (
            '<section class="okf-recent" id="okf-recent-rail" '
            'aria-labelledby="okf-recent-heading">'
            '<h2 class="okf-recent__label" id="okf-recent-heading">Recently changed</h2>'
            f'<ol class="okf-recent__list">{"".join(recent_items)}</ol></section>'
        )
    # Group by type.
    by_type: dict[str, list[Concept]] = {}
    for c in direct:
        by_type.setdefault(c.type or "<untyped>", []).append(c)

    recommended_ids = {
        "tutorials/install": 0,
        "tutorials/live_studio_basics": 1,
        "reference/cli": 2,
        "reference/spec": 3,
        "demo/annotations_v2_showcase": 4,
    }

    # Per-concept link counts for the card footers (cheap: bundle.graph()
    # is cached on the bundle). Fail soft — cards render without counts.
    graph = None
    try:
        graph = bundle.graph()
    except Exception:  # noqa: BLE001 — index must render even if links are broken
        graph = None

    groups_parts: list[str] = []
    type_nav_entries: list[tuple[str, str, str, str]] = []  # (href, icon, label, count)
    # Allocate GUARANTEED-unique section ids across the full type set
    # (deterministic collision-counter, not a probabilistic hash).
    section_ids = _allocate_type_section_ids(sorted(by_type))
    for t in sorted(by_type):
        # iter2 P3-3: add c.id tiebreaker so two concepts with the same
        # lowercased title are ordered deterministically (§3.2), not by
        # Bundle dict-insertion order.
        items = sorted(
            by_type[t],
            key=lambda c: (
                recommended_ids.get(concept_id_to_str(c.id), 100),
                "" if c.timestamp is None else str(c.timestamp),
                c.title.lower(), c.id,
            ),
        )
        color = palette.get(t, type_color_for(t))
        icon = type_icon_svg(t, size=15, cls="okf-type-icon")
        section_id = f"okf-type-{section_ids[t]}"
        type_nav_entries.append((section_id, icon, t, str(len(items))))
        rows = []
        for c in items:
            cid_str = concept_id_to_str(c.id)
            # Static sub-index pages live at <sub>/index.html, so concept
            # URLs must be relativised against that location (a root-
            # relative "explanation/x.html" would resolve to
            # explanation/explanation/x.html from there).
            sub_source = tuple(sub.split("/")) + ("index",) if sub else None
            url = url_for_concept(c.id, mode, source_cid=sub_source)
            desc = _esc(c.description[:140] + ("…" if len(c.description) > 140 else ""))
            # Card footer: outgoing/incoming counts + first few tags.
            meta_bits: list[str] = []
            if graph is not None:
                n_out = len([l for l in graph.outlinks(c.id) if l.target is not None])
                n_in = len(graph.backlinks(c.id))
                if n_out or n_in:
                    meta_bits.append(
                        f'<span class="okf-card__links" title="{n_out} outgoing, {n_in} incoming links">'
                        f'→ {n_out} · ← {n_in}</span>'
                    )
            for tag in c.tags[:3]:
                meta_bits.append(f'<span class="okf-tag okf-tag--card">{_esc(tag)}</span>')
            meta_html = (
                f'<span class="okf-card__meta">{"".join(meta_bits)}</span>'
                if meta_bits else ""
            )
            active_count = comment_counts.get(cid_str, 0)
            if comment_summary is not None and isinstance(active_count, int) and active_count > 0:
                label = f'{active_count} active comment{"" if active_count == 1 else "s"}'
                comment_link = (
                    f'<a class="okf-card__comments okf-comment-count" href="{_esc_attr_qs(url)}" '
                    f'data-concept-id="{_esc_attr_qs(cid_str)}" '
                    f'aria-label="{_esc_attr_qs(label + " on " + c.title)}">{_esc(label)}</a>'
                )
                if meta_html:
                    meta_html = meta_html[:-7] + comment_link + "</span>"
                else:
                    meta_html = f'<span class="okf-card__meta">{comment_link}</span>'
            # The li keeps the .okf-concept-list li a contract (first anchor
            # = concept link; studio.js stampConceptIds + presence rely on
            # it) — the card look is CSS on top. The type chip (icon +
            # textual label) is a <span>, NOT an <a>, so the first <a>
            # remains the concept link. The icon inherits the type accent
            # colour via a wrapper span; the textual label uses the AA-safe
            # muted text token (not the raw accent — small text needs 4.5:1).
            description_html = f' <span class="okf-concept-desc okf-card__desc">{desc}</span>' if desc else ""
            rows.append(
                f'<li class="okf-card" data-concept-id="{_esc_attr_qs(cid_str)}" '
                f'data-index-type="{_esc_attr_qs(t)}" data-index-title="{_esc_attr_qs(c.title.casefold())}" '
                # F7: the client recency sort reads the metadata owner's
                # effective instant (canonical UTC), not the raw legacy key.
                f'data-index-instant="{_esc_attr_qs(effective_instant_key(c.frontmatter))}" '
                f'data-index-recommended="{recommended_ids.get(cid_str, 100)}" style="--okf-type-accent:{_esc(color)}">'
                f'<span class="okf-card__type">'
                f'<span class="okf-card__type-icon" style="color:{_esc(color)}">{type_icon_svg(t, size=13, cls="okf-type-icon")}</span>'
                f'<span class="okf-card__type-label">{_esc(t)}</span>'
                f'</span>'
                f'<a href="{_esc(url)}" class="okf-internal okf-card__link">{_esc(c.title)}</a>'
                f' <span class="okf-muted okf-card__id">{_esc(cid_str)}</span>'
                f'{description_html}'
                f'{meta_html}'
                f'</li>'
            )
        groups_parts.append(
            f'<section class="okf-section" id="{section_id}" data-index-section data-index-type="{_esc_attr_qs(t)}" style="--okf-type-accent:{_esc(color)}">'
            f'<h2 class="okf-section__title"><span class="okf-section__icon" style="color:{_esc(color)}">{icon}</span>'
            f'{_esc(t)} <span class="okf-section__count">{len(items)}</span></h2>'
            f'<ul class="okf-concept-list okf-cardgrid">{"".join(rows)}</ul>'
            f'</section>'
        )

    type_options = "".join(
        f'<option value="{_esc_attr_qs(t)}">{_esc(t)}</option>' for t in sorted(by_type)
    )
    browse_controls = ""
    if not sub:
        browse_controls = (
            '<section class="okf-index-browse" aria-labelledby="okf-index-browse-title">'
            '<div class="okf-index-browse__heading"><h2 id="okf-index-browse-title">What do you want to explore?</h2>'
            '<p>Every concept appears once in the inventory below.</p></div>'
            '<form class="okf-index-browse__controls" data-index-browse-controls>'
            '<label>Order <select data-index-order><option value="recommended" selected>Recommended</option>'
            '<option value="recent">Recently changed</option><option value="title">Title</option></select></label>'
            f'<label>Type <select data-index-type><option value="">All types</option>{type_options}</select></label>'
            '<button type="button" class="okf-btn" data-index-clear>Clear</button>'
            '<span class="okf-visually-hidden" role="status" aria-live="polite" data-index-status></span>'
            '</form></section>'
            '<p class="okf-index-state" data-index-no-match hidden>No concepts match this type. Clear the filter to browse everything.</p>'
            + ('<p class="okf-index-state" data-index-empty>No concepts are available yet. Add a Markdown concept, then rebuild the index.</p>' if not direct else '<p data-index-empty hidden></p>')
        )

    # Subdirectory links: any directory strictly deeper than `sub` whose
    # immediate parent is `sub`.
    sub_dirs: set[str] = set()
    for c in bundle.concepts.values():
        if not sub:
            if len(c.id) > 1:
                sub_dirs.add(c.id[0])
        else:
            sp = sub_parts
            if len(c.id) > len(sp) + 1 and c.id[:len(sp)] == sp:
                sub_dirs.add("/".join(c.id[:len(sp) + 1]))
    subdir_parts: list[str] = []
    for d in sorted(sub_dirs):
        url = f"{('../' * (len(sub_parts))) if sub_parts else ''}{d}/" if mode == "static" else f"/{d}/"
        # For static, link to the directory's index.html.
        if mode == "static":
            rel = "../" * len(sub_parts) + d + "/index.html"
        else:
            rel = "/" + d + "/"
        subdir_parts.append(
            f'<a href="{_esc(rel)}"><strong>{_esc(d)}</strong>'
            f'<span class="okf-muted">directory</span></a>'
        )
    subdirs_html = (
        f'<div class="okf-subdirs">{"".join(subdir_parts)}</div>'
        if subdir_parts else ""
    )

    # Compact type-navigation: a horizontal bar of jump links (icon + type
    # name + count) that anchor-scroll to each type section. On the ROOT
    # index this REPLACES the bulky subdirs tile grid (which duplicated the
    # type sections' coverage) as the primary pre-content navigation; the
    # hero (title/intro/stats/graph CTA) is preserved. On subdir indexes
    # the subdirs are retained (deeper-directory navigation) and the
    # type-nav is prepended to the groups so it sits between hero and cards.
    if type_nav_entries:
        nav_links = "".join(
            f'<a href="#{_esc(sid)}" class="okf-type-nav__link">'
            f'{icon}<span class="okf-type-nav__label">{_esc(label)}</span>'
            f'<span class="okf-type-nav__count">{count}</span>'
            f'</a>'
            for sid, icon, label, count in type_nav_entries
        )
        type_nav_html = (
            f'<nav class="okf-type-nav" aria-label="Jump to concept type">'
            f'{nav_links}'
            f'</nav>'
        )
        if not sub:
            # Root index: the type-nav IS the pre-content navigation; the
            # old subdirs tiles duplicated the type sections' coverage.
            subdirs_html = type_nav_html
        else:
            # Subdir index: keep subdirs (deeper-dir navigation) and prepend
            # the type-nav so both are available above the card grids.
            groups_parts.insert(0, type_nav_html)

    theme_attr = ""
    configured_theme, configured_contrast, configured_border = _theme_preference(config)
    initial_theme = _resolve_initial_theme(configured_theme)
    initial_mode = (
        "auto" if configured_theme == "auto" else configured_theme.rsplit("-", 1)[1]
    )
    theme_attr = (f' data-theme="{initial_theme}"'
                  + _theme_modifier_attrs(configured_contrast, configured_border))

    # P1-10: emit data-okf-enhance on index pages too so wiki.js detects
    # static mode and gates typed-query live suggestions there (was only on
    # concept pages, so the gate never covered the index/search surfaces).
    revision = bundle_revision(bundle)
    # Do not pre-publish a terminal capture state. The document lifecycle
    # owner publishes readiness after renderers/theme/Studio have settled.
    route_source = tuple(sub.split("/")) + ("index",) if sub else None
    concept_routes = {
        concept_id_to_str(concept.id): url_for_concept(
            concept.id, mode, source_cid=route_source,
        )
        for concept in bundle.concepts.values()
    }
    routes_json = json.dumps(concept_routes, sort_keys=True, separators=(",", ":"))
    data_attrs = (f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}" '
                  f'data-okf-studio="{("1" if mode == "serve" else "0")}" '
                  f'data-okf-revision="{_esc_attr_qs(revision)}" data-okf-capture-revision="{_esc_attr_qs(revision)}" '
                  f'data-okf-concept-routes="{_esc_attr_qs(routes_json)}"')

    css_link = f'<link rel="stylesheet" href="{static_prefix}/wiki.css">'
    shell_css_link = f'<link rel="stylesheet" href="{static_prefix}/shell.css">'
    # theme.js in <head> before first paint (no FOUC); see concept page.
    head_theme_js = (
        "" if mode == "serve"
        else f'<script src="{static_prefix}/theme.js"></script>'
    )
    js_link = (f'<script src="{static_prefix}/capture-readiness.js" defer></script>\n'
               f'<script src="{static_prefix}/wiki.js" defer></script>\n'
               f'<script src="{static_prefix}/index-browse.js" defer></script>')
    renderers_link = f'<script src="{static_prefix}/renderers.js" defer></script>'
    graph_link = "/__graph" if mode in ("serve", "spa") else (
        ("../" * len(sub_parts)) + "__graph.html" if sub_parts else "__graph.html"
    )

    # P2-61: shared topbar nav. Index link points to the current directory
    # root (acts as a reload when already on the index). Graph link uses
    # the page-relative path computed above.
    root_prefix_for_nav = ("../" * len(sub_parts)) if sub_parts else ""
    if mode in ("serve", "spa"):
        root_prefix_for_nav = "/"  # P1-3 (iter-5): always root-absolute for serve/spa
    nav_html = _nav_controls_html(
        root_prefix=root_prefix_for_nav,
        graph_link=graph_link,
        initial_theme=initial_theme,
        initial_mode=initial_mode,
        initial_contrast=configured_contrast,
        initial_border=configured_border,
        mode=mode,
    )

    # iter1 P3-11: brand consistency — the index page brand is a real link to
    # the bundle root (matching the other 3 templates). On the root index it
    # is a self-link with aria-current="page"; on subdir indexes it links back
    # to the root so a deep user can climb back up.
    if mode in ("serve", "spa"):
        brand_href = "/"
    else:
        depth = len(sub.split("/")) if sub else 0
        brand_href = ("../" * depth) + "index.html" if depth else "index.html"
    brand_aria = ' aria-current="page"' if not sub else ""
    shell_html = _application_shell_html(
        name=name,
        bundle_href=brand_href,
        search_href=f"{root_prefix_for_nav}__search{'.html' if mode == 'static' else ''}",
        index_href=brand_href,
        graph_href=graph_link,
        current="index",
        mode=mode,
        initial_theme=initial_theme,
        initial_mode=initial_mode,
        initial_contrast=configured_contrast,
        initial_border=configured_border,
    )

    return (
        template
        .replace("__LANG__", "en")
        .replace("__THEME_ATTR__", theme_attr)
        .replace("__DATA_ATTRS__", data_attrs)
        .replace("__HEAD_TITLE__", _esc(title))
        .replace("__BUNDLE_NAME__", _esc(name))
        .replace("__BRAND_HREF__", _esc_attr_qs(brand_href))
        .replace("__BRAND_ARIA_CURRENT__", brand_aria)
        .replace("__STATIC_PREFIX__", static_prefix)
          .replace("__WIKI_CSS_LINK__", css_link)
          .replace("__SHELL_CSS_LINK__", shell_css_link)
          .replace("__THEME_CONFIG__", _theme_config_html(
              configured_theme, configured_contrast, configured_border,
          ))
          .replace("__THEME_HEAD_JS__", head_theme_js)
        .replace("__WIKI_JS_LINK__", _type_icons_block() + js_link)
        .replace("__RENDERERS_JS_LINK__", renderers_link)
        .replace("__NAV_HTML__", nav_html)
        .replace("__APPLICATION_SHELL__", shell_html)
        .replace("__INDEX_TITLE__", _esc(title))
        .replace('__INDEX_TITLE_ANCHOR__', title_anchor)
          .replace("__HERO_MODIFIER__", hero_modifier)
          .replace("__HERO_V2__", hero_v2)
          .replace("__INDEX_INTRO__", intro)
          .replace("__INDEX_INTRO_REMAINDER__", intro_remainder)
          .replace("__INDEX_OVERVIEW__", ('<details class="okf-index__overview"><summary>Authored overview</summary><div class="okf-prose">' + intro_remainder + '</div></details>') if intro_remainder.strip() else '')
          .replace("__INDEX_ENGAGEMENT__", engagement_html)
          .replace("__HERO_STATS__", hero_stats)
          .replace("__GRAPH_THUMBNAIL__", graph_thumbnail)
        .replace("__GROUPS_HTML__", "\n".join(groups_parts))
        .replace("__INDEX_BROWSE_CONTROLS__", browse_controls)
        .replace("__SUBDIRS_HTML__", subdirs_html)
        .replace("__GLOBAL_GRAPH_LINK__", _esc(graph_link))
    )


def _section_bootstrap_script(navigation: dict[str, Any] | None) -> str:
    """Canonical, bounded ``<script type="application/json">`` transport.

    The exact serialization is part of the transport contract: sorted keys,
    compact separators, ASCII-only (``\\uXXXX``), ``<`` escaped. Clients
    re-canonicalize the parsed value and reject any mismatch, which refuses
    duplicate keys, reordering, whitespace drift, and non-ASCII smuggling in
    one deterministic check. Over-bound payloads degrade to ``null`` — the
    absent-metadata fallback — never a partial map.
    """
    if navigation:
        payload = json.dumps(navigation, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":")).replace('<', '\\u003c')
    else:
        payload = "null"
    if len(payload) > MAX_SECTION_NAVIGATION_BYTES:
        payload = "null"
    return ('<script type="application/json" id="okf-section-bootstrap">'
            + payload + '</script>')


def _rendered_outline_script(body_html: str, concept: str,
                             revision: str) -> str:
    """Bounded renderer-owned heading map embedded with one concept page.

    Admission proof for the destination side: the payload names the page's
    canonical concept identity and the concept's per-document
    normalized-source revision, and the displayed heading must still carry
    the exact label this renderer emitted. Plugins or scripts that move,
    rename, or duplicate the heading fail the label/identity check and the
    reader stays at the document root.
    """
    headings = rendered_heading_labels(body_html)
    data: dict[str, Any] = {"schema": 1, "concept": concept,
                            "source_revision": revision}
    if len(headings) <= 1000:
        data["headings"] = headings
    payload = json.dumps(data, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).replace('<', '\\u003c')
    if len(payload) > 128 * 1024:
        payload = json.dumps(
            {"schema": 1, "concept": concept, "source_revision": revision},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return ('<script type="application/json" id="okf-rendered-outline">'
            + payload + '</script>')


def _single_source_revisions_script(bundle: Bundle) -> str:
    """Bounded per-concept rendered-source revision map for the single-file viewer.

    Destination-side binding for section intents inside the frozen snapshot:
    canonical concept identity -> :func:`annotations.concept_content_rev` of
    the concept's LF-normalized raw source — the SAME normalized source the
    embedded bodies were rendered from (never served CRLF bytes, never the
    corpus revision). Independent of the search transport: a section intent
    whose claimed revision does not equal this map's entry for the target
    concept never binds, and a missing or over-bound map emits ``null`` so
    section actions degrade to ordinary selection. A canonical sibling
    script, never a BUNDLE key.
    """
    from .annotations import concept_content_rev
    from .navigation import (MAX_SECTION_NAVIGATION_BYTES,
                             MAX_SECTION_NAVIGATION_ENTRIES)

    revisions: dict[str, str] = {}
    for cid, concept in sorted(bundle.concepts.items()):
        if len(revisions) >= MAX_SECTION_NAVIGATION_ENTRIES:
            revisions = {}
            break
        revisions[concept_id_to_str(cid)] = concept_content_rev(
            concept.raw_text.replace("\r\n", "\n"))
    payload = json.dumps({"schema": 1, "revisions": revisions},
                         ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).replace('<', '\\u003c')
    if len(payload) > MAX_SECTION_NAVIGATION_BYTES:
        payload = "null"
    return ('<script type="application/json" id="okf-single-source-revisions">'
            + payload + '</script>')


def _render_search_page(
    bundle: Bundle,
    *,
    mode: str,
    name: str,
    config: dict[str, Any],
    query: str,
    results: list[dict[str, Any]],
    request: Any = None,
    error: Any = None,
    sections: dict[str, str] | None = None,
    section_navigation: dict[str, Any] | None = None,
    now: Any = None,
) -> str:
    from datetime import datetime
    from .retrieval import bundle_revision

    # U4: freshness in the result advisory group is evaluated once, when the
    # results render; an open page does not re-evaluate it later.
    now = now or datetime.now(timezone.utc)
    template = load_template("search_page.html", bundle)
    if mode != 'serve':
        template = inject_review_metadata(template, config.get('review_base_url'))
    # R1/R2: exported workbenches carry the bounded, revision-bound
    # section-destination metadata as separate sibling bootstrap JSON. Strict
    # passage/corpus records are unchanged; the client re-canonicalizes and
    # validates the shape once per snapshot, re-binds it to the loaded corpus
    # revision per result, and re-admits every heading inside the actually
    # displayed document (absent metadata → root reading).
    if mode in ("static", "spa"):
        template = template.replace(
            '</head>', _section_bootstrap_script(section_navigation)
            + '</head>', 1)
    static_prefix = "/__static" if mode in ("serve", "spa") else "__static"
    back_link = "/" if mode in ("serve", "spa") else "index.html"

    req_mode = getattr(request, "mode", None) or "lexical"
    req_limit = getattr(request, "limit", None) or 20
    filters = {
        "type": getattr(request, "type_filter", None),
        "tag": getattr(request, "tag", None),
        "entity": getattr(request, "entity", None),
        "relation": getattr(request, "relation", None),
        "source": getattr(request, "source", None),
        "target": getattr(request, "target", None),
    }
    if req_mode == "entity" and filters["entity"] and query == filters["entity"]:
        # SearchRequest normalization promotes an entity-only seed into q for
        # retrieval. Keep the authored UI state filter-only and avoid emitting
        # a duplicate q+entity pair back into forms/context links.
        query = ""
    active_filters = [(key, value) for key, value in filters.items() if value]
    def manual_context_href(*, concept: str | None = None) -> str:
        if mode == "static":
            return f"__context/{concept}.txt" if concept else "index.html"
        values: dict[str, Any] = {
            "format": "markdown", "q": query, "mode": req_mode,
            "limit": req_limit, "budget_tokens": 6000, "neighbors": 1,
        }
        values.update({key: value for key, value in filters.items() if value})
        if concept:
            values["concept"] = concept
        # Canonical concept identifiers are slash-delimited paths.  The live
        # parser intentionally rejects encoded separators, so preserve literal
        # '/' while percent-encoding all other unsafe characters.
        return "/__context?" + urlencode(values, safe="/")
    error_values = dict(getattr(error, "fields", {}) or {})
    error_fields = set(error_values)
    def marked_excerpt(value: str) -> str:
        value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
        value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
        value = re.sub(r"^\s{0,3}(?:#{1,6}|[-*+] |>+)\s*", "", value, flags=re.MULTILINE)
        value = re.sub(r"```[^\n]*\n?|~~~[^\n]*\n?|```|~~~", "", value)
        value = re.sub(r"`([^`\n]+)`", r"\1", value)
        value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
        value = re.sub(r"(^|\s)\*([^*\n]+)\*(?=\s|[.,;:!?]|$)", r"\1\2", value)
        terms = list(dict.fromkeys(re.findall(r"[\w-]+", query, flags=re.UNICODE)))[:32]
        if not terms:
            return _esc(value)
        pattern = re.compile("(" + "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True)) + ")", re.IGNORECASE)
        return "".join(f"<mark>{_esc(part)}</mark>" if index % 2 else _esc(part)
                       for index, part in enumerate(pattern.split(value)))

    def structured_excerpt(value: str) -> str:
        lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
        if len(lines) >= 2 and all(line.startswith("|") and line.endswith("|") for line in lines):
            rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
            if rows[1] and all(re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]):
                head, body = rows[0], rows[2:]
                heading = "".join(f"<th scope=\"col\">{marked_excerpt(cell)}</th>" for cell in head)
                body_html = "".join("<tr>" + "".join(
                    f'<td data-label="{_esc_attr_qs(head[i] if i < len(head) else "Value")}">{marked_excerpt(cell)}</td>'
                    for i, cell in enumerate(row)
                ) + "</tr>" for row in body)
                return f'<div class="okf-search-table-wrap"><table class="okf-search-table"><thead><tr>{heading}</tr></thead><tbody>{body_html}</tbody></table></div>'
        return f'<blockquote class="okf-search-snippet okf-search-snippet--compact">{marked_excerpt(value)}</blockquote>'

    mode_labels = (
        ("hybrid", "Best overall", "Exact and related ideas"),
        ("lexical", "Exact wording", "Literal terms and identifiers"),
        ("semantic", "Similar meaning", "Related wording, no embeddings"),
        ("tag", "Tagged with", "Concept tags"),
        ("entity", "Mentions entity", "Entity labels and aliases"),
        ("relation", "Connected by relation", "Graph source, target, or relation"),
    )
    mode_controls_parts: list[str] = []
    for value, label, help_text in mode_labels:
        mode_controls_parts.append(
            '<label class="okf-search-mode">'
            f'<input type="radio" name="mode" value="{value}"'
            f'{" checked" if value == req_mode else ""}> '
            f'<span>{label}<small>{help_text}</small></span></label>'
        )
    mode_controls = "".join(mode_controls_parts)
    type_values = sorted({c.type for c in bundle.concepts.values() if c.type})
    tag_values = sorted({tag for c in bundle.concepts.values() for tag in c.tags})
    concept_values = sorted(concept_id_to_str(c.id) for c in bundle.concepts.values())
    def datalist(identifier: str, values: list[str]) -> str:
        return f'<datalist id="{identifier}">' + "".join(
            f'<option value="{_esc_attr_qs(value)}"></option>' for value in values
        ) + "</datalist>"
    def text_filter(key: str, label: str, list_id: str | None = None) -> str:
        list_attr = f' list="{list_id}"' if list_id else ""
        value = _esc_attr_qs(str(filters.get(key) or ""))
        error_id = f"okf-search-{key}-error"
        invalid = f' aria-invalid="true" aria-describedby="{error_id}"' if key in error_fields else ""
        message = f'<small class="okf-field-error" id="{error_id}">{_esc(error_values[key])}</small>' if key in error_fields else ""
        return (f'<label class="okf-field" for="okf-search-{key}"><span>{label}</span><input id="okf-search-{key}" name="{key}"'
                f' value="{value}" maxlength="256"{list_attr}{invalid}>{message}</label>')
    limit_error_html = (f'<small class="okf-field-error" id="okf-search-limit-error">{_esc(error_values["limit"])}</small>'
                        if "limit" in error_fields else "")
    query_error_html = (f'<small class="okf-field-error" id="okf-search-q-error">{_esc(error_values["q"])}</small>'
                        if "q" in error_fields else "")
    limit_invalid = ' aria-invalid="true" aria-describedby="okf-search-limit-error"' if "limit" in error_fields else ""
    filter_fields = (
        text_filter("type", "Type", "okf-search-types") +
        text_filter("tag", "Tag", "okf-search-tags") +
        f'<div data-mode-field="entity">{text_filter("entity", "Entity")}</div>' +
        f'<div data-mode-field="relation">{text_filter("relation", "Relation")}{text_filter("source", "Source concept", "okf-search-concepts")}{text_filter("target", "Target concept", "okf-search-concepts")}</div>' +
        '<label class="okf-field" for="okf-search-limit"><span>Result limit</span>'
        f'<input id="okf-search-limit" type="number" name="limit" min="1" max="100" value="{_esc_attr_qs(str(req_limit))}"'
        f'{limit_invalid}>'
        f'{limit_error_html}</label>'
    )
    clear_href = "/__search" if mode in ("serve", "spa") else "__search.html"
    query_invalid = 'aria-invalid="true" aria-describedby="okf-search-q-error" ' if "q" in error_fields else ""
    # U8: a collapsed "Filters, 0 active" summary spends a number on nothing.
    # The count is meaningful only once it is nonzero — zero renders as a
    # plain "Filters" disclosure (visible and accessible naming identical),
    # and the native <details> disclosure itself is untouched.
    filters_label = (
        f"Filters, {len(active_filters)} active" if active_filters else "Filters"
    )
    search_form_html = (
        f'<form action="{"/__search-form" if mode == "serve" else clear_href}" method="get" role="search" aria-label="Search this bundle" class="okf-search-workbench">'
        '<div class="okf-search-sticky-controls">'
        '<div class="okf-search-query"><label for="okf-search-query">Search terms</label>'
        f'<div><input id="okf-search-query" type="search" name="q" maxlength="4096" value="{_esc_attr_qs(query)}" '
        f'{query_invalid}'
        'autocomplete="off"><button type="submit" class="okf-search-submit">Search</button></div>'
        f'{query_error_html}</div>'
        '<fieldset id="okf-search-mode" class="okf-search-modes"><legend>How should results be matched?</legend>'
        f'{mode_controls}</fieldset></div>'
        f'<details class="okf-search-filters"{" open" if active_filters else ""}><summary>{filters_label}</summary>'
        f'<div class="okf-search-filter-grid">{filter_fields}</div>'
        '<div class="okf-search-filter-actions"><button type="submit" class="okf-btn">Apply</button>'
        f'<a class="okf-btn" href="{clear_href}">Clear</a></div></details>'
        f'{datalist("okf-search-types", type_values)}{datalist("okf-search-tags", tag_values)}'
        f'{datalist("okf-search-concepts", concept_values)}</form>'
    )

    try:
        palette = resolve_palette(bundle)
    except Exception:  # noqa: BLE001 — results degrade to accentless
        palette = {}
    result_parts: list[str] = []
    for r in results:
        cid = r.get("concept_id") or r.get("id") or ""
        title = r.get("title") or cid
        evidence = r.get("evidence") or {}
        raw_passage = evidence.get("passage")
        has_passage = isinstance(raw_passage, dict) and bool(raw_passage)
        passage = raw_passage if has_passage else {}
        desc = passage.get("excerpt") or ""
        url = ("/" + cid) if mode in ("serve", "spa") else (cid + ".html")
        ctype = r.get("type") or ""
        if not ctype:
            try:
                from .paths import concept_id_from_str as _cid_from_str
                c = bundle.concepts.get(_cid_from_str(cid)) if cid else None
                ctype = (c.type or "") if c else ""
            except Exception:  # noqa: BLE001 — type chip is decorative
                ctype = ""
        color = palette.get(ctype, type_color_for(ctype))
        icon = type_icon_svg(ctype, size=14, cls="okf-type-icon")
        type_chip = (
            f'<span class="okf-search-result__type" style="--okf-type-accent:{_esc(color)}">'
            f'{icon} {_esc(ctype)}</span>' if ctype else ""
        )
        heading_suffix = f' <span class="okf-muted">{_esc(passage.get("heading"))}</span>' if passage.get("heading") else ""
        citation = str(passage.get("citation") or "")
        path = str(passage.get("path") or cid)
        reasons = r.get("reasons") or []
        reason_html = "".join(f"<li>{_esc(reason)}</li>" for reason in reasons[:8])
        detail = r.get("detail") or {}
        # U4: only non-default trust/lifecycle/freshness states, inline on
        # the muted meta line (never inside the <h3>).
        advisory = render_search_advisory(r.get("metadata") or {}, now)
        why_html = (
            '<div class="okf-search-ranking"><h4>Why did this result rank here?</h4><dl>'
            f'<dt>Method</dt><dd>{_esc(", ".join(r.get("backends") or [r.get("source_backend", "")]))}</dd>'
            f'<dt>Score</dt><dd>{_esc(r.get("score", ""))}</dd>'
            f'<dt>Evidence</dt><dd><ul>{reason_html}</ul></dd>'
            '</dl></div>'
        )
        evidence_summary = (
            'Exact passage'
            if has_passage else
            'Graph evidence only'
        )
        passage_preview = (
            structured_excerpt(str(desc)[:2000]) if has_passage else
            '<p class="okf-search-concept-rationale">Graph evidence only. No exact passage is available for this concept-level result.</p>'
        )
        exact_size = len(str(desc)[:2000]) + len(citation) + 2
        cited_action = (
            f'<button type="button" class="okf-btn okf-js-action" data-search-action="copy-passage" data-copy-passage data-excerpt="{_esc_attr_qs(str(desc)[:2000])}" data-citation="{_esc_attr_qs(citation)}" aria-label="Copy passage from {_esc_attr_qs(title)}">Copy passage</button>'
            if has_passage else ""
        )
        passage_scope = (
            f'<p class="okf-copy-scope">Source: <code>{_esc(path)}</code> · Scope: exact passage · Estimated size: {exact_size} characters.</p>'
            if has_passage else ""
        )
        exact_evidence = (
            '<section class="okf-search-exact" aria-label="Exact passage evidence">'
            '<h4>What does the source say?</h4>'
            f'<pre class="okf-search-source" aria-label="Exact canonical source passage">{_esc(str(desc)[:2000])}</pre>'
            f'<p class="okf-search-evidence"><code class="okf-search-citation">{_esc(citation)}</code><span class="okf-search-path">{_esc(path)}</span></p>'
            '</section>' if has_passage else
            '<p class="okf-search-no-passage">No exact passage or line citation is available. This result is supported only by canonical graph metadata.</p>'
        )
        concept_scope = (
            f'<p class="okf-copy-scope">Source: <code>{_esc(cid)}</code> · Scope: one concept with bounded related evidence · Estimated size: calculated before copy.</p>'
        )
        # R1: the distinct matching-section action appears only when the
        # server proved a source-owned, revision-bound heading destination for
        # this row's actual textual evidence passage (see server.py). The
        # href carries a bounded navigation INTENT (destination id + expected
        # page source revision + heading-label binding), never the raw
        # fragment: the browser has nothing to native-scroll to until the
        # target page admits the destination and publishes the real hash
        # itself (replaceState, no second history entry). Ordinary Open stays
        # document-root; graph-only, preamble, ambiguous, and unprovable rows
        # keep exactly one Open.
        entry = (sections or {}).get(cid) if has_passage else None
        section_href = None
        if entry:
            section_href = (
                f'{_esc(url)}?{INTENT_PARAM_FRAGMENT}='
                f'{_esc_attr_qs(_url_quote(entry["fragment"], safe=""))}'
                f'&{INTENT_PARAM_REVISION}='
                f'{_esc_attr_qs(_url_quote(entry["source_revision"], safe=""))}'
                f'&{INTENT_PARAM_CONCEPT}='
                f'{_esc_attr_qs(_url_quote(entry["concept"], safe=""))}'
                f'&{INTENT_PARAM_HASH}='
                f'{section_intent_hash(entry["heading"])}'
            )
        section_action = (
            f'<a class="okf-btn okf-search-open-section" href="{section_href}">'
            'Open matching section</a>'
            if section_href else ""
        )
        result_parts.append(
            f'<li><article class="okf-search-result" style="--okf-type-accent:{_esc(color)}" data-concept-id="{_esc_attr_qs(cid)}">'
            f'<h3><a href="{_esc(url)}" class="okf-internal">{_esc(title)}</a>{heading_suffix}</h3>'
            # iter1 P3-13: concept-id moved OUT of the <h3> so the heading
            # outline announces only the title (screen-reader heading-list
            # navigation no longer reads "title concept/id" as one string).
            f'<div class="okf-search-result__meta okf-muted">{type_chip} {_esc(cid)}{advisory}</div>'
            f'{passage_preview}'
            f'<p class="okf-search-evidence-summary">{_esc(evidence_summary)}</p>'
            '<div class="okf-search-result__actions">'
              f'<a class="okf-btn okf-btn--primary okf-search-open" href="{_esc(url)}">Open</a>'
              f'{section_action}'
              f'{cited_action}'
            f'</div>{passage_scope}'
            '<details class="okf-search-disclosure"><summary>Evidence and context</summary>'
              f'{exact_evidence}{why_html}'
              '<div class="okf-search-advanced-actions">'
                f'{concept_scope}<button type="button" class="okf-btn okf-context-copy okf-js-action" data-search-action="copy-concept-context" data-context-concept="{_esc_attr_qs(cid)}" aria-label="Copy concept context for {_esc_attr_qs(title)}">Copy concept context</button>'
                f'<div class="okf-nojs-manual"><a class="okf-btn" href="{_esc_attr_qs(manual_context_href(concept=cid))}">{"Open emitted concept-only Markdown" if mode == "static" else "Open canonical Markdown context"}</a></div>'
              '</div>'
            f'</details></article></li>'
        )
    if error is not None:
        field_control_ids = {"q": "okf-search-query", "type_filter": "okf-search-type"}
        field_rows = "".join(
            f'<li><a href="#{_esc_attr_qs(field_control_ids.get(str(k), "okf-search-" + str(k)))}">'
            f'{_esc(k)}: {_esc(v)}</a></li>' for k, v in sorted(error.fields.items())
        )
        results_html = (f'<div class="okf-search-error" role="alert" data-okf-error-code="{_esc_attr_qs(error.code)}">'
                        f'<h3>Search request error</h3><p>{_esc(error.message)}</p><ul>{field_rows}</ul>'
                        f'<p><a class="okf-btn" href="{clear_href}">Start a new search</a> <a href="{back_link}">Browse the index</a></p></div>')
    elif result_parts:
        results_html = '<ol class="okf-search-results-list">' + "\n".join(result_parts) + '</ol>'
    elif not query and not active_filters:
        # F7: the empty-query suggestion head follows the metadata owner's
        # effective change instant, not the raw legacy ``timestamp`` key.
        recent = _recent_concepts_by_instant(bundle, limit=5)
        recent_html = "".join(f'<li><a href="{("/" if mode in ("serve", "spa") else "")}{_esc(concept_id_to_str(c.id))}{".html" if mode == "static" else ""}">{_esc(c.title)}</a> <span class="okf-muted">{_esc(c.type or "concept")}</span></li>' for c in recent)
        browse_types = " ".join(f'<a class="okf-tag" href="{clear_href}?type={_esc_attr_qs(t)}">{_esc(t)}</a>' for t in type_values[:12])
        browse_tags = " ".join(f'<a class="okf-tag" href="{clear_href}?tag={_esc_attr_qs(t)}">{_esc(t)}</a>' for t in tag_values[:12])
        results_html = (f'<div class="okf-search-initial"><section><h3>Recent concepts</h3><ul>{recent_html}</ul></section>'
                        f'<section><h3>Browse</h3><p>{browse_types}</p><p>{browse_tags}</p></section>'
                        '<section><h3>Try a search</h3><p><a href="?q=architecture&amp;mode=hybrid">architecture</a> or <a href="?q=decision&amp;mode=semantic">meaning of a decision</a></p></section></div>')
    else:
        results_html = (f'<div class="okf-search-empty"><h3>No matching passages</h3><p>No evidence matched “{_esc(query)}” with the current filters.</p>'
                        f'<p><a class="okf-btn" href="{clear_href}?q={_esc_attr_qs(query)}">Clear filters</a> <a href="{back_link}">Browse the index</a></p></div>')

    theme_attr = ""
    configured_theme, configured_contrast, configured_border = _theme_preference(config)
    initial_theme = _resolve_initial_theme(configured_theme)
    initial_mode = (
        "auto" if configured_theme == "auto" else configured_theme.rsplit("-", 1)[1]
    )
    theme_attr = (f' data-theme="{initial_theme}"'
                  + _theme_modifier_attrs(configured_contrast, configured_border))

    # P1-10: emit data-okf-enhance on the search page too so wiki.js
    # detects static mode there as well (the search page body also carries
    # okf-viewer--search, excluding it from the typed-query live gate that
    # static-search.js owns for real).
    # Search runtime owns loading/ready/unavailable transitions; the initial
    # document therefore leaves data-okf-capture-ready unpublished.
    data_attrs = (
        f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}" '
        f'data-okf-studio="{("1" if mode == "serve" else "0")}" '
        f'data-okf-capture-surface="search" '
        f'data-okf-capture-query="{_esc_attr_qs(query)}" data-okf-revision="{_esc_attr_qs(bundle_revision(bundle))}"'
    )

    css_link = f'<link rel="stylesheet" href="{static_prefix}/wiki.css">'
    shell_css_link = f'<link rel="stylesheet" href="{static_prefix}/shell.css">'
    # theme.js in <head> before first paint (no FOUC); see concept page.
    head_theme_js = (
        "" if mode == "serve"
        else f'<script src="{static_prefix}/theme.js"></script>'
    )
    js_link = (f'<script src="{static_prefix}/capture-readiness.js" defer></script>\n'
               f'<script src="{static_prefix}/wiki.js" defer></script>')
    renderers_link = f'<script src="{static_prefix}/renderers.js" defer></script>'
    # Current spec §9: in static mode also load the client-side
    # searcher so the static __search.html page is functional (no /__search
    # backend available). The live `serve`/`spa` paths are unchanged — they
    # keep hitting /__search server-side. The static-search.js script is
    # emitted under __static/ by _emit_site (it iterates list_builtin_static).
    if mode in ("static", "spa"):
        js_link = (
              f'<script src="{static_prefix}/capture-readiness.js" defer></script>\n'
              f'<script src="{static_prefix}/portable-retrieval.js" defer></script>\n'
            f'<script src="{static_prefix}/wiki.js" defer></script>\n'
            f'<script src="{static_prefix}/static-search.js" defer></script>'
        )

    # P2-61: shared topbar nav. Search page lives at the bundle root, so
    # the form posts back to itself and Graph/Index are root-relative.
    root_prefix_for_nav = "/" if mode in ("serve", "spa") else "./"
    graph_link = "/__graph" if mode in ("serve", "spa") else "__graph.html"
    nav_html = _nav_controls_html(
        root_prefix=root_prefix_for_nav,
        graph_link=graph_link,
        initial_theme=initial_theme,
        initial_mode=initial_mode,
        initial_contrast=configured_contrast,
        initial_border=configured_border,
        current_query=query,
        mode=mode,
    )
    shell_html = _application_shell_html(
        name=name,
        bundle_href=back_link,
        search_href=clear_href,
        index_href=back_link,
        graph_href=graph_link,
        current="search",
        mode=mode,
        initial_theme=initial_theme,
        initial_mode=initial_mode,
        initial_contrast=configured_contrast,
        initial_border=configured_border,
        search_query=query,
    )

    # iter1 P3-13: proper pluralisation (no "result(s)" cop-out). The heading
    # also carries the query so the empty state reads naturally.
    count = len(results)
    if error is not None:
        heading = "Search could not run"
    elif not query and not active_filters:
        heading = "Find evidence and build context"
    elif not query and active_filters and count == 0:
        heading = "No results for current filters"
    elif not query and active_filters:
        heading = f"{count} result{'s' if count != 1 else ''} for current filters"
    elif count == 0:
        heading = f'No results for &ldquo;{_esc(query)}&rdquo;'
    elif count == 1:
        heading = f'1 result for &ldquo;{_esc(query)}&rdquo;'
    else:
        heading = f'{count} results for &ldquo;{_esc(query)}&rdquo;'

    active_filters_html = ('<p class="okf-active-filters">' + ", ".join(
        f'<span>{_esc(k)}: {_esc(v)}</span>' for k, v in active_filters
    ) + '</p>') if active_filters else ""
    page_context_html = ""
    if count and (query or active_filters):
        page_context_html = (
            f'<div class="okf-page-context" role="group" aria-label="Context for first {count} results">'
            '<details><summary>Build context pack for first '
            f'{count} results</summary><div class="okf-context-options">'
              '<p class="okf-context-scope-note">Selected cited passages for reuse — not an AI summary. Source: current Search URL · Scope: current query and result set · Estimated size: calculated before copy.</p>'
              '<div class="okf-js-action">'
              '<details class="okf-context-advanced"><summary>Advanced: budget and related passages</summary>'
              '<label>Token budget <input type="number" data-context-budget min="128" max="50000" value="6000"></label>'
              '<label>Neighbors <select data-context-neighbors><option value="0">None</option><option value="1" selected>1 hop</option><option value="2">2 hops</option></select></label>'
              f'<label>Included limit <input type="number" data-context-limit min="1" max="100" value="{int(req_limit)}"></label>'
              '</details>'
              '<p class="okf-muted" data-context-estimate data-default-text="Authoritative size and truncation are checked when the context pack is prepared.">Authoritative size and truncation are checked when the context pack is prepared.</p>'
                f'<button type="button" class="okf-btn okf-js-action" data-search-action="build-context-pack" data-context-prepare data-context-query="{_esc_attr_qs(query)}" data-context-mode="{_esc_attr_qs(req_mode)}">Build context pack</button>'
                '<button type="button" class="okf-btn okf-js-action" data-context-copy-prepared disabled>Copy context pack</button>'
              '</div>'
                f'<a class="okf-btn okf-nojs-manual" href="{_esc_attr_qs(manual_context_href())}">{"Browse concepts for emitted context" if mode == "static" else "Open default canonical Markdown context"}</a>'
            '</div></details></div>'
        )
    return (
        template
        .replace("__LANG__", "en")
        .replace("__THEME_ATTR__", theme_attr)
        .replace("__DATA_ATTRS__", data_attrs)
        .replace("__HEAD_TITLE__", _esc(f"Search: {query} — {name}"))
        .replace("__BUNDLE_NAME__", _esc(name))
        .replace("__STATIC_PREFIX__", static_prefix)
          .replace("__WIKI_CSS_LINK__", css_link)
          .replace("__SHELL_CSS_LINK__", shell_css_link)
          .replace("__THEME_CONFIG__", _theme_config_html(
              configured_theme, configured_contrast, configured_border,
          ))
          .replace("__THEME_HEAD_JS__", head_theme_js)
        .replace("__WIKI_JS_LINK__", _type_icons_block() + js_link)
        .replace("__RENDERERS_JS_LINK__", renderers_link)
        .replace("__NAV_HTML__", nav_html)
        .replace("__APPLICATION_SHELL__", shell_html)
        .replace("__QUERY_ESCAPED__", _esc_attr_qs(query))
        .replace("__QUERY_HTML__", _esc(query))
        .replace("__RESULTS_HTML__", results_html)
        .replace("__RESULTS_HEADING__", heading)
        .replace("__SEARCH_FORM_HTML__", search_form_html)
        .replace("__ACTIVE_FILTERS_HTML__", active_filters_html)
        .replace("__PAGE_CONTEXT_HTML__", page_context_html)
        .replace("__BACK_LINK__", back_link)
    )


def _esc_attr_qs(s: str) -> str:
    """Escape a query for safe interpolation into an HTML attribute value."""
    return (s.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;"))


def _render_graph_page(
    bundle: Bundle,
    *,
    mode: str,
    name: str,
    config: dict[str, Any],
    offline_assets: Any = None,
) -> str:
    template = load_template("graph_page.html", bundle)
    if mode != 'serve':
        template = inject_review_metadata(template, config.get('review_base_url'))
    static_prefix = "/__static" if mode in ("serve", "spa") else "__static"
    data_url = "/__data/graph.json" if mode in ("serve", "spa") else "__data/graph.json"
    back_link = "/" if mode in ("serve", "spa") else "index.html"
    search_link = "/__search" if mode in ("serve", "spa") else "__search.html"
    configured_theme, configured_contrast, configured_border = _theme_preference(config)
    initial_theme = _resolve_initial_theme(configured_theme)
    initial_mode = (
        "auto" if configured_theme == "auto" else configured_theme.rsplit("-", 1)[1]
    )
    initial_layout = config.get("default_layout") or "cose"
    theme_attr = (f' data-theme="{initial_theme}"'
                  + _theme_modifier_attrs(configured_contrast, configured_border))

    # F7: the no-JS suggestion head follows the metadata owner's effective
    # change instant (dated first, newest first; unknown-time last, stable
    # concept-id order), not the raw legacy ``timestamp`` key.
    recent = _recent_concepts_by_instant(bundle, limit=5)
    recent_items = "".join(
        '<li>'
        f'<a href="{_esc_attr_qs(("/" if mode in ("serve", "spa") else "") + concept_id_to_str(concept.id) + (".html" if mode == "static" else ""))}">'
        f'{_esc(concept.title)}</a>'
        f'<span class="okf-muted">{_esc(concept.type or "concept")}</span>'
        '</li>'
        for concept in recent
    )
    graph_data = build_graph_data(bundle, name=name)
    nojs_relationships = "".join(
        "<li>" + _esc(relation["label"]) + " — " + _esc(relation["type"])
        + ": " + _esc(relation["source"]) + " → " + _esc(relation["target"]) + "</li>"
        for edge in graph_data["edges"] for relation in edge["data"]["relations"]
    )
    nojs_graph_body = (
        '<section class="okf-graph-nojs" aria-labelledby="okf-graph-nojs-title">'
        '<p class="okf-graph-nojs__eyebrow">Graph view</p>'
        '<h1 id="okf-graph-nojs-title">Graph unavailable without JavaScript</h1>'
        '<p class="okf-graph-nojs__lead">The interactive relationship map needs JavaScript. '
        'You can still browse every authored concept from the index or open a recent concept below.</p>'
        '<figure class="okf-graph-nojs__export" aria-labelledby="okf-graph-nojs-export-caption">'
        f'{graph_export_svg(graph_data)}'
        '<figcaption id="okf-graph-nojs-export-caption">Static relationship map. Every relationship is also listed below.</figcaption></figure>'
        f'<p><a class="okf-btn okf-graph-nojs__action" href="{_esc_attr_qs(back_link)}">Browse the concept index</a></p>'
        '<section class="okf-graph-nojs__recent" aria-labelledby="okf-graph-nojs-recent-title">'
        '<h2 id="okf-graph-nojs-recent-title">Recent concepts</h2>'
        f'<ul>{recent_items}</ul>'
        '</section><section aria-labelledby="okf-graph-nojs-relations-title">'
        '<h2 id="okf-graph-nojs-relations-title">Resolved in-bundle relationships</h2>'
        f'<ol>{nojs_relationships}</ol></section></section>'
    )

    cdn_scripts = _CDN_SCRIPTS if config.get("cdn", True) else (
        "<!-- CDN scripts omitted (config.cdn=false). -->"
    )
    if offline_assets is not None:
        # X1: the same pinned chain, from verified local copies, in order.
        cdn_scripts = offline_assets.graph_scripts(static_prefix)

    # P1-3: emit data-okf-mode on the graph page body so graph.js can
    # detect static mode and append .html to the Open-page href (the
    # concept pages are emitted as <id>.html; the extensionless URL 404s
    # in static builds). Mirrors the __DATA_ATTRS__ wiring on the
    # concept/index/search pages.
    revision = bundle_revision(bundle)
    data_attrs = (f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}" '
                  f'data-okf-studio="{("1" if mode == "serve" else "0")}" '
                  f'data-okf-revision="{_esc_attr_qs(revision)}" data-okf-capture-revision="{_esc_attr_qs(revision)}" data-okf-capture-ready="loading"')

    css_link = f'<link rel="stylesheet" href="{static_prefix}/wiki.css">'
    shell_css_link = f'<link rel="stylesheet" href="{static_prefix}/shell.css">'
    graph_css_link = f'<link rel="stylesheet" href="{static_prefix}/graph.css">'
    # theme.js in <head> before first paint (no FOUC); see concept page.
    head_theme_js = (
        "" if mode == "serve"
        else f'<script src="{static_prefix}/theme.js"></script>'
    )
    # The graph detail panel renders the same
    # server-produced concept HTML as the wiki pages, so it needs the SAME
    # progressive renderers (mermaid/hljs/KaTeX). graph.js dispatches
    # okf-loom:bodyPatched after each showDetail() so renderers.js re-scans.
    graph_js = (
          f'<script src="{static_prefix}/capture-readiness.js" defer></script>\n' +
          (f'<script src="{static_prefix}/portable-retrieval.js" defer></script>\n'
         if mode in ("static", "spa") else "") +
        f'<script src="{static_prefix}/graph.js" defer></script>\n'
        f'<script src="{static_prefix}/renderers.js" defer></script>'
    )
    shell_html = _application_shell_html(
        name=name,
        bundle_href=back_link,
        search_href=search_link,
        index_href=back_link,
        graph_href=("/__graph" if mode in ("serve", "spa") else "__graph.html"),
        current="graph",
        mode=mode,
        initial_theme=initial_theme,
        initial_mode=initial_mode,
        initial_contrast=configured_contrast,
        initial_border=configured_border,
    )

    return (
        template
        .replace("__LANG__", "en")
        .replace("__THEME_ATTR__", theme_attr)
        .replace("__DATA_ATTRS__", data_attrs)
        .replace("__HEAD_TITLE__", _esc(f"Graph — {name}"))
        .replace("__BUNDLE_NAME__", _esc(name))
        .replace("__STATIC_PREFIX__", static_prefix)
        .replace("__WIKI_CSS_LINK__", css_link)
          .replace("__SHELL_CSS_LINK__", shell_css_link)
          .replace("__GRAPH_CSS_LINK__", graph_css_link)
          .replace("__THEME_CONFIG__", _theme_config_html(
              configured_theme, configured_contrast, configured_border,
          ))
          .replace("__THEME_HEAD_JS__", head_theme_js)
        .replace("__GRAPH_JS_SRC__", graph_js)
        .replace("__CDN_SCRIPTS__", cdn_scripts)
        .replace("__INITIAL_THEME__", initial_theme)
          .replace("__INITIAL_THEME_BUTTON__", _appearance_control_html(
              initial_theme, initial_mode, configured_contrast, configured_border,
          ))
        .replace("__APPLICATION_SHELL__", shell_html)
        .replace("__INITIAL_LAYOUT__", initial_layout)
        .replace("__GRAPH_DATA_URL__", data_url)
        .replace("__BACK_LINK__", back_link)
        .replace("__SEARCH_LINK__", search_link)
        .replace("__NOJS_GRAPH_BODY__", nojs_graph_body)
    )


# ---------------------------------------------------------------------------
# Atomic write helpers — migrated to io_utils (shared, symlink-safe,
# mkstemp-based). The former local copies are deleted; io_utils.atomic_
# write_text/_bytes/atomic_replace_tree are the canonical implementations.
# ---------------------------------------------------------------------------

from .io_utils import atomic_write_text as _atomic_write_text
from .io_utils import atomic_write_bytes as _atomic_write_bytes
