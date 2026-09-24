r"""Small pure-Python Markdown renderer for the OKF viewer.

Scope: the subset used in OKF bundles — headings, paragraphs, unordered /
ordered lists (incl. nested), task lists, blockquotes, fenced code blocks,
inline code, GFM-style tables (incl. column alignment and ``\|`` escaped
pipes), links, images, autolinks (``<https://…>`` and bare URLs),
emphasis + strong, strikethrough, hard line breaks (trailing two spaces or
``\``), single-line footnotes (``[^id]`` / ``[^id]: text``), horizontal
rules. This is NOT a full CommonMark implementation; it is a deliberate
minimal subset (no markdown package dependency, per the hard constraints).

Raw HTML is ESCAPED, not passed through. All link ``href`` and image
``src`` attributes pass through a URL-scheme allowlist (see
``_safe_url``); ``javascript:``, ``data:``, ``vbscript:``, and other
dangerous schemes are neutralised.

Two entry points:

* :func:`markdown_to_html` — pure text→HTML, links rendered verbatim.
* :func:`rewrite_internal_links` — post-process rendered HTML, replacing
  ``<a href="<internal>">`` with the viewer's per-mode URL form using a
  precomputed ``target_raw → url`` map (the Graph already resolved the
  target concept id, so the caller builds the map from ``Graph.out_edges``).

Why split: the renderer stays free of Bundle/Graph coupling (so it can be
unit-tested in isolation), and link rewriting is a single policy applied
identically by ``render.py`` (static / single-file data) and ``server.py``.
"""
from __future__ import annotations

import html as _html
import posixpath
import re
from typing import Iterator, Mapping

from ..paths import ConceptId, concept_id_to_str
from ..markdown_blocks import (
    parse_markdown_blocks,
    block_visible_segments,
    table_cell_spans,
    generate_owner_nonce,
    find_inline_code_span,
    _MARKER_ATTR as _PROJ_OWNER_ATTR,
)

# ---------------------------------------------------------------------------
# Block-level scanner
# ---------------------------------------------------------------------------

# P1-1 (iter-8): CommonMark §4.5 defines BOTH ``` and ~~~ as code fences.
# parse.py:_FENCE_RE already handles both; the renderer must match.
_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>```+|~~~+)(?P<lang>[^\n]*)\n(?P<code>.*?)\n[ \t]*(?P=fence)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?$", re.MULTILINE)
_HR_RE = re.compile(r"^[ \t]*(-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")

# Anchors / fragment links (passed through untouched).
_LINK_HREF_RE = re.compile(r'(<a\s+[^>]*?)href=("|\')(?P<href>[^"\']+)\2', re.IGNORECASE)


def _slugify(text: str) -> str:
    text = _html.unescape(text).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text or "section"


# ---------------------------------------------------------------------------
# Inline formatting
# ---------------------------------------------------------------------------

_EMPH_RES: list[tuple[re.Pattern[str], str]] = [
    # Strong first (longest match), then emphasis.
    (re.compile(r"\*\*(.+?)\*\*"), "<strong>\\1</strong>"),
    (re.compile(r"__(.+?)__"), "<strong>\\1</strong>"),
    (re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])"), "<em>\\1</em>"),
    (re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)_(?![\w_])"), "<em>\\1</em>"),
    # GFM strikethrough. After strong/em so ~~a **b** c~~ nests correctly.
    (re.compile(r"~~(?!\s)(.+?)(?<!\s)~~"), "<del>\\1</del>"),
]

# GFM autolinks. Angle form takes any allowlisted scheme; the bare form is
# restricted to http(s) and must not fire inside markdown link syntax — the
# lookbehind excludes positions right after ``(`` (that is ``](href``),
# ``[`` (labels), quotes (titles) and word chars (schemes mid-token).
_AUTOLINK_ANGLE_RE = re.compile(
    r"<(?P<url>(?:https?|ftp)://[^\s<>]+|mailto:[^\s<>]+|tel:[^\s<>]+)>"
)
_AUTOLINK_EMAIL_RE = re.compile(
    r"<(?P<addr>[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)>"
)
_AUTOLINK_BARE_RE = re.compile(
    r"(?<![(\[\"'=\w])(?P<url>https?://[^\s<>()\"']+)"
)

# Footnote references: [^id]. Substituted BEFORE the link regex so a ref is
# never mistaken for a link label (mirrors GFM precedence).
_FOOTNOTE_REF_RE = re.compile(r"\[\^(?P<id>[^\]\s]+)\]")
# Footnote definition line: [^id]: text (block-level, single line).
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^(?P<id>[^\]\s]+)\]:\s+(?P<text>.+)$")


def _footnote_slug(fid: str) -> str:
    """Sanitize a footnote id for use inside an HTML id attribute."""
    return re.sub(r"[^\w-]", "-", fid)


def _extract_inline_code(text: str, stash_code) -> str:
    """Replace every inline code span in ``text`` with a stash placeholder.

    Uses the shared CommonMark delimiter grammar
    (:func:`okf_loom.markdown_blocks.find_inline_code_span`) — the same
    function the projection text tokenizer uses — so the rendered visible
    text and the annotation projection agree by construction: an opening
    backtick run of N closes at the next run of exactly N backticks, and a
    failed opener run is literal text scanned past whole (F16).
    """
    parts: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find("`", i)
        if j < 0:
            parts.append(text[i:])
            break
        span = find_inline_code_span(text, j)
        if span is None:
            # No equal-length closer: the whole opener run is literal text.
            open_end = j + 1
            while open_end < n and text[open_end] == "`":
                open_end += 1
            parts.append(text[i:open_end])
            i = open_end
            continue
        open_start, code_start, code_end, span_end = span
        parts.append(text[i:open_start])
        parts.append(stash_code(text[code_start:code_end]))
        i = span_end
    return "".join(parts)


def _render_inline(text: str, *, _escaped: bool = False) -> str:
    """Render inline markdown to HTML.

    Inline code spans are extracted first into placeholders so emphasis /
    link regexes never touch their contents, then restored at the end.
    Autolinks are rendered from the RAW (pre-escape) text and stashed the
    same way, so their hrefs are never entity-mangled.

    ``_escaped=True`` is the internal recursion form used for link labels:
    the text has already been HTML-escaped by the outer call, so escaping
    (and raw-text autolinking) must not run a second time — re-escaping was
    the historical ``&`` → ``&amp;amp;`` double-escape bug.
    """
    stash: list[str] = []

    def _stash(rendered: str) -> str:
        token = f"\x01{len(stash)}\x01"
        stash.append(rendered)
        return token

    def _stash_code(code: str) -> str:
        return _stash(f"<code>{_escape(code)}</code>")

    # Pull out inline code with the shared delimiter grammar (see
    # _extract_inline_code) so emphasis / link regexes never touch its
    # contents and the projection tokenizer sees identical spans.
    text = _extract_inline_code(text, _stash_code)

    # Preserve well-formed character references through the general escaping
    # pass. Browsers decode these to one rendered character; the annotation
    # projection maps that rendered boundary back to the full canonical token.
    # The hex marker is case-insensitive (``#x`` / ``#X``) to match the forms
    # a browser decodes — otherwise ``&#X3a;`` would slip past and be judged a
    # relative URL instead of a colon (SEC: entity-encoded scheme bypass).
    if not _escaped:
        text = re.sub(
            r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});",
            lambda m: _stash(m.group(0)), text,
        )

    def _decode_for_url(raw: str) -> str:
        """Decode a captured link/image destination to its browser-semantic
        form *before* URL-safety validation.

        Character references are stashed as ``\\x01N\\x01`` placeholders
        before the link/image regex runs (so they survive the escape pass
        for visible text). Without restoring them here, an entity-encoded
        scheme such as ``javascript&colon;`` / ``&#58;`` / ``&#x3a;`` /
        ``&#X3A;`` reaches ``_safe_url`` still carrying a placeholder — or,
        for forms the stash regex misses, a doubled ``&amp;…`` — and is
        judged relative rather than as ``javascript:``.

        Restoring the stashed entity then HTML-unescaping once mirrors the
        single attribute decode a browser performs on the emitted href/src,
        so ``_safe_url`` judges the exact destination the browser resolves.
        Safe query/path entities (``?a=1&amp;b=2``) decode to their
        browser-semantic form (``?a=1&b=2``) and remain valid for the
        allowlisted scheme; double-encoded forms (``&amp;colon;``) are NOT
        over-decoded into executable schemes. The decoded value is re-escaped
        by ``_escape_attr`` on emit, so no HTML injection is possible.
        """
        for i in range(len(stash)):
            raw = raw.replace(f"\x01{i}\x01", stash[i])
        return _html.unescape(raw)

    def _autolink(url: str, label: str | None = None) -> str | None:
        safe = _safe_url(url)
        if safe is None:
            return None
        return _stash(
            f'<a href="{_escape_attr(safe)}" class="okf-external" '
            f'rel="noopener noreferrer" target="_blank">{_escape(label or url)}</a>'
        )

    if not _escaped:
        # Autolinks run on raw text: <https://…>, <user@host>, bare http(s).
        def _angle(m: re.Match[str]) -> str:
            out = _autolink(m.group("url"))
            return out if out is not None else m.group(0)

        def _email(m: re.Match[str]) -> str:
            addr = m.group("addr")
            out = _autolink("mailto:" + addr, addr)
            return out if out is not None else m.group(0)

        def _bare(m: re.Match[str]) -> str:
            url = m.group("url")
            # Trailing sentence punctuation belongs to the prose, not the URL.
            trimmed = url.rstrip(".,;:!?")
            if not trimmed:
                return url
            out = _autolink(trimmed)
            if out is None:
                return url
            return out + url[len(trimmed):]

        text = _AUTOLINK_ANGLE_RE.sub(_angle, text)
        text = _AUTOLINK_EMAIL_RE.sub(_email, text)
        text = _AUTOLINK_BARE_RE.sub(_bare, text)

        # Escape the rest.
        text = _escape(text, escape_quotes=False)

    # Footnote references (before links, so [^1] is never a link label).
    def _footnote_ref(m: re.Match[str]) -> str:
        slug = _footnote_slug(m.group("id"))
        return (
            f'<sup class="okf-footnote-ref" id="fnref-{_escape_attr(slug)}">'
            f'<a href="#fn-{_escape_attr(slug)}">[{m.group("id")}]</a></sup>'
        )

    text = _FOOTNOTE_REF_RE.sub(_footnote_ref, text)

    # Images: ![alt](src) — MUST run before the link regex, which would
    # otherwise consume the [alt](src) part and leave a stray "!<a…>"
    # (the historical broken-images bug). alt/src come from escaped text;
    # src is decoded to its browser-semantic form (entity placeholders
    # restored + HTML-unescaped) so _safe_url sees the real destination and
    # entity-encoded schemes (javascript&colon; / &#58; / &#x3a; / &#X3a;)
    # cannot sneak through as relative-looking strings. alt keeps its
    # entities for visible-text display.
    text = re.sub(
        r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)\)",
        lambda m: _stash(_img_tag(_html.unescape(m.group("alt")),
                                  _decode_for_url(m.group("src")))),
        text,
    )

    # Links: [label](href "title?")
    def _link(m: re.Match[str]) -> str:
        label = _render_inline(m.group("label"), _escaped=True)
        # The regex ran over escaped text that still carries stashed entity
        # placeholders; decode to the browser-semantic destination (restore
        # placeholders + HTML-unescape once) so _safe_url judges the real
        # URL — entity-encoded schemes (javascript&colon; / &#58; / &#x3a; /
        # &#X3a;) decode to ``javascript:`` and are blocked instead of
        # looking relative. The single decode also lets the _escape_attr
        # below emit a correctly (singly) escaped href.
        href = _decode_for_url(m.group("href").strip())
        title = _html.unescape((m.group("title") or "").strip().strip('"'))
        # URL scheme allowlist (defence against javascript:/data:/vbscript: XSS).
        safe_href = _safe_url(href)
        if safe_href is None:
            # Disallowed scheme: render label as plain text (no anchor).
            return f'<span class="okf-link-blocked" title="blocked: { _escape_attr(href) }">{label}</span>'
        attrs = f' href="{_escape_attr(safe_href)}"'
        if title:
            attrs += f' title="{_escape_attr(title)}"'
        # Auto-mark internal vs external (server/render rewrites href later).
        if _looks_internal(safe_href):
            attrs += ' class="okf-internal"'
        else:
            attrs += ' class="okf-external" rel="noopener noreferrer" target="_blank"'
        return f"<a{attrs}>{label}</a>"

    text = re.sub(
        r"\[(?P<label>[^\]]*)\]\((?P<href>[^)\s]+)(?:\s+\"(?P<title>[^\"]*)\")?\)",
        _link,
        text,
    )

    # Emphasis / strong / strikethrough.
    for pat, repl in _EMPH_RES:
        text = pat.sub(repl, text)

    # Restore stashed inline code / autolinks / images. A stashed fragment
    # may itself contain an earlier token (nesting), so resolve repeatedly.
    for i in reversed(range(len(stash))):
        text = text.replace(f"\x01{i}\x01", stash[i])
    return text


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------

# Allowed URL schemes for href/src attributes. Anything else is neutralised.
# This list is the OWASP "safe URL" subset for links/images in user content.
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({
    "http", "https", "mailto", "tel", "ftp",
})

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")


def _safe_url(href: str) -> str | None:
    """Return a safe-to-emit href, or None if the scheme is disallowed.

    Allowed:
        - Explicit schemes in ``_ALLOWED_URL_SCHEMES``.
        - Anchor-only links (``#section``).
        - Site-relative absolute paths (``/foo``).
        - Relative paths (``./foo``, ``../foo``, ``foo.md``).

    Disallowed (returns None):
        - ``javascript:``, ``data:``, ``vbscript:``, ``file:``, and any
          other scheme not in the allowlist. These would allow XSS when
          rendered into an ``<a href>`` or ``<img src>`` attribute.
        - URLs containing ASCII control characters (TAB, LF, CR, NUL, etc.)
          which browsers strip before scheme resolution and could be used
          to smuggle ``javascript:`` past the scheme check.
        - U+FFFD (REPLACEMENT CHARACTER): this is what ``&#0;`` (NUL)
          decodes to in both Python and browsers; a NUL smuggled into a
          scheme (``javascript&#0;:``) must fail closed rather than emit an
          anchor carrying the ``javascript`` text.
    """
    if not href:
        return None
    # Strip leading/trailing whitespace (browsers do this before resolution).
    href = href.strip()
    if not href:
        return None
    # Reject any ASCII control characters (0x00-0x20, 0x7f) which browsers
    # strip from URLs before scheme resolution. Without this, an href like
    # "java\tscript:alert(1)" passes the scheme regex (the tab breaks the
    # match) but the browser resolves it to "javascript:alert(1)".
    # U+FFFD is rejected too: it is the decoded form of ``&#0;`` (NUL) and
    # has no place in a legitimate scheme/URL origin.
    if any(ord(c) <= 0x20 or ord(c) == 0x7f or c == "\ufffd" for c in href):
        return None
    # Anchor-only.
    if href.startswith("#"):
        return href
    # Absolute site path.
    if href.startswith("/"):
        # Reject "//host" protocol-relative URLs (could be confused with a
        # scheme; browsers treat them as same-protocol).
        if href.startswith("//"):
            return None
        return href
    # Relative path.
    if href.startswith("./") or href.startswith("../"):
        return href
    # Bare relative (e.g. "foo.md", "foo/bar.md"). Treat as relative iff
    # there is no scheme before any path separator.
    m = _SCHEME_RE.match(href)
    if m:
        scheme = m.group(1).lower()
        if scheme in _ALLOWED_URL_SCHEMES:
            return href
        return None
    # No scheme detected and does not start with `/`, `./`, `../`, `#`:
    # treat as relative (e.g. "foo.md", "image.png"). This is the
    # conventional markdown case for sibling files.
    return href


def _img_tag(alt: str, src: str) -> str:
    safe_src = _safe_url(src)
    if safe_src is None:
        return (
            f'<span class="okf-img-blocked" title="blocked: '
            f'{_escape_attr(src)}">{_escape(alt)}</span>'
        )
    return (
        f'<img alt="{_escape_attr(alt)}" src="{_escape_attr(safe_src)}" '
        f'loading="lazy" />'
    )


def _looks_internal(href: str) -> bool:
    if not href:
        return False
    if href.startswith("#"):
        return True  # same-document anchor; no new browsing context
    if "://" in href or href.startswith("//"):
        return False
    if href.startswith("mailto:") or href.startswith("tel:"):
        return False
    path = href.split("#", 1)[0]
    return path.endswith(".md") or path.startswith("/") or path.startswith("./") or path.startswith("../")


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

def _escape(s: str, *, escape_quotes: bool = False) -> str:
    s = (s.replace("&", "&amp;")
          .replace("<", "&lt;")
          .replace(">", "&gt;"))
    if escape_quotes:
        s = s.replace('"', "&quot;")
    return s


def _escape_attr(s: str) -> str:
    return _escape(s, escape_quotes=True)


# ---------------------------------------------------------------------------
# Block renderer
# ---------------------------------------------------------------------------

def _render_fenced(code: str, lang: str, indent: str) -> str:
    lang_clean = lang.strip().lower()
    # Mermaid diagrams: render as <div class="mermaid"> so the CDN-loaded
    # mermaid.js can pick them up. Degrades to the code block text if the
    # CDN is unreachable (the div still shows the source, just unstyled).
    if lang_clean == "mermaid":
        return f'<div class="mermaid">{_escape(code)}</div>'
    # Math (KaTeX-style): mark for client-side rendering. Degrades to
    # the raw LaTeX if KaTeX isn't loaded.
    if lang_clean in ("math", "katex", "latex"):
        return f'<div class="math">{_escape(code)}</div>'
    cls = f' class="language-{_escape_attr(lang.strip())}"' if lang.strip() else ""
    return f"<pre><code{cls}>{_escape(code)}</code></pre>"


def _split_table_row(line: str) -> list[str]:
    return [line[a:b].replace("\\|", "|") for a, b in table_cell_spans(line)]


def _render_table(header: str, rows: list[str], sep: str = "",
                  tr_nonces: list[str] | None = None) -> str:
    # GFM column alignment from the separator row: ``:---`` left (default),
    # ``:---:`` center, ``---:`` right. Emitted as classes so the stylesheet
    # (not inline styles) owns the presentation.
    aligns: list[str] = []
    if sep:
        for cell in _split_table_row(sep):
            if cell.startswith(":") and cell.endswith(":") and len(cell) > 1:
                aligns.append(' class="okf-al-c"')
            elif cell.endswith(":"):
                aligns.append(' class="okf-al-r"')
            else:
                aligns.append("")

    header_cells = _split_table_row(header) if header else []
    # GFM: body rows are padded/truncated to the header width. This also
    # keeps every column rectangular for the client-side sorter/resizer.
    ncols = len(header_cells)

    def _cls(idx: int) -> str:
        return aligns[idx] if idx < len(aligns) else ""

    def _row(cells: list[str], tag: str, nonce: str | None = None) -> str:
        if ncols:
            cells = (cells + [""] * ncols)[:ncols]
        rendered = "".join(
            f"<{tag}{_cls(i)}>{_render_inline(c)}</{tag}>"
            for i, c in enumerate(cells)
        )
        attr = f' {_PROJ_OWNER_ATTR}="{nonce}"' if nonce else ""
        return f"<tr{attr}>{rendered}</tr>"

    out = ['<table class="okf-table">']
    if header:
        out.append("<thead>")
        h_nonce = tr_nonces[0] if tr_nonces else None
        out.append(_row(header_cells, "th", h_nonce))
        out.append("</thead>")
    out.append("<tbody>")
    for ri, r in enumerate(rows):
        d_nonce = tr_nonces[ri + 1] if tr_nonces and ri + 1 < len(tr_nonces) else None
        out.append(_row(_split_table_row(r), "td", d_nonce))
    out.append("</tbody>")
    out.append("</table>")
    return "\n".join(out)


_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+[.)])\s+(?P<text>.*)$")
# GFM task-list item body: "[ ] text" / "[x] text" right after the marker.
_TASK_ITEM_RE = re.compile(r"^\[(?P<state>[ xX])\]\s+(?P<text>.*)$", re.DOTALL)


def _render_list_item_text(text: str) -> str:
    """Render one list item's text, honouring GFM task-list syntax.

    Returns the inner HTML for the ``<li>`` (checkbox + text for tasks).
    The checkbox is disabled: OKF's directing model is "the user comments,
    the agent edits", so state changes flow through markdown edits.
    """
    m = _TASK_ITEM_RE.match(text)
    if not m:
        return _render_inline(text)
    checked = ' checked=""' if m.group("state") in "xX" else ""
    return (
        f'<input type="checkbox" class="okf-task__box" disabled=""{checked} /> '
        f"{_render_inline(m.group('text'))}"
    )


def _render_list_block(lines: list[str], li_nonces: list[str] | None = None) -> str:
    """Render a contiguous run of list-item lines as a nested list."""
    # Each item: (indent_depth, ordered, text, children_lines)
    root_items: list[dict] = []
    stack: list[dict] = []  # each dict: {ordered, items: [...]}

    def _depth(line: str) -> int:
        m = _LIST_ITEM_RE.match(line)
        return len(m.group("indent").expandtabs(4)) if m else 0

    for line in lines:
        m = _LIST_ITEM_RE.match(line)
        if not m:
            # Continuation line: non-marker text that belongs to the current
            # item (e.g. a soft-wrapped paragraph inside a list item). Append
            # it to the current item's text after a newline so the <li>
            # textContent matches the IR's continuation-line handling.
            if stack and line.strip():
                stack[-1]["node"]["text"] += "\n" + line.strip()
            continue
        depth = _depth(line)
        ordered = not m.group("marker") in "-*+"
        node = {"ordered": ordered, "text": m.group("text"), "children": []}

        # Pop stack while top has >= depth.
        while stack and stack[-1]["depth"] >= depth:
            stack.pop()

        if not stack:
            root_items.append(node)
        else:
            stack[-1]["node"]["children"].append(node)
        stack.append({"depth": depth, "node": node})

    # Attach owner nonces to root items (one per top-level list item).
    if li_nonces:
        for idx, node in enumerate(root_items):
            if idx < len(li_nonces):
                node["_owner_nonce"] = li_nonces[idx]

    _MAX_LIST_NESTING = 32

    def _emit(node: dict, _d: int = 0) -> str:
        tag = "ol" if node["ordered"] else "ul"
        li_cls = ' class="okf-task"' if _TASK_ITEM_RE.match(node["text"]) else ""
        owner_attr = ""
        if _d == 0 and node.get("_owner_nonce"):
            owner_attr = f' {_PROJ_OWNER_ATTR}="{node["_owner_nonce"]}"'
        body = f"<li{li_cls}{owner_attr}>{_render_list_item_text(node['text'])}"
        if node["children"] and _d < _MAX_LIST_NESTING:
            # Join nested sibling <li> elements with "\n" so the rendered
            # textContent contains a separator between successive nested
            # items, matching the IR's _emit_newline policy between
            # successive list-marker lines (both LF and CRLF sources
            # normalise to a single visible "\n").
            child_items = "\n".join(_emit(c, _d + 1) for c in node["children"])
            body += f"\n<{tag}>{child_items}</{tag}>"
        elif node["children"]:
            # At the cap: flatten remaining children as inline text so the
            # content is still readable without recursing further.
            flat = "; ".join(
                _render_list_item_text(child["text"])
                for child in _iter_all_children(node)
            )
            body += f" ({flat})"
        body += "</li>"
        return body

    def _iter_all_children(node: dict) -> Iterator[dict]:
        """Yield every descendant of ``node`` in document order (BFS)."""
        queue = list(node["children"])
        while queue:
            n = queue.pop(0)
            yield n
            queue.extend(n["children"])

    if not root_items:
        return ""
    # Group consecutive root items by their `ordered` flag so adjacent
    # unordered + ordered lists become separate <ul>/<ol> elements.
    out_parts: list[str] = []
    current_tag = "ol" if root_items[0]["ordered"] else "ul"
    current_items: list[str] = []
    for node in root_items:
        tag = "ol" if node["ordered"] else "ul"
        if tag != current_tag:
            out_parts.append(f"<{current_tag}>{''.join(current_items)}</{current_tag}>")
            current_items = []
            current_tag = tag
        current_items.append(_emit(node))
    out_parts.append(f"<{current_tag}>{''.join(current_items)}</{current_tag}>")
    return "\n".join(out_parts)


def _process_para_lines(text: str) -> str:
    """Process paragraph block text into the renderer's joined payload.

    Strips each line, detects hard breaks (trailing 2+ spaces or backslash),
    and joins with single spaces — exactly matching the legacy line scanner.
    """
    parts: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue
        hard_break = bool(re.search(r"(  +|\\)$", line))
        if stripped.endswith("\\"):
            stripped = stripped[:-1].rstrip()
        parts.append(stripped + ("\x05" if hard_break else ""))
    return " ".join(parts)


# A render unit: (kind, payload, ir_blocks). ir_blocks is the list of
# underlying canonical MarkdownBlock(s) for owner-marker assignment.
_RenderUnit = tuple[str, str, list]


def _split_blocks(text: str) -> list[_RenderUnit]:
    """Split text into render units backed by the canonical block IR.

    This is a **thin adapter** over :func:`parse_markdown_blocks`; it does NOT
    independently parse headings/lists/quotes/tables/fences. Each unit carries
    the underlying IR block(s) so the renderer can assign owner markers.

    Kinds: 'fence', 'heading', 'hr', 'table', 'list', 'quote', 'footnote',
    'para'. Payload formats are identical to the legacy scanner so
    :func:`markdown_to_html` rendering is unchanged.
    """
    from ..markdown_blocks import MarkdownBlock
    ir = parse_markdown_blocks(text)
    units: list[_RenderUnit] = []
    i = 0
    while i < len(ir):
        block = ir[i]
        bt = block.text
        if block.kind == "heading":
            m = re.match(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?(?:\r?\n)?$", bt)
            if m:
                level, title = len(m.group(1)), m.group(2).rstrip(" \t")
            else:
                first_nl = bt.find("\n")
                title_line = (bt if first_nl < 0 else bt[:first_nl]).rstrip("\r")
                title = title_line.strip()
                level = 1 if bt.lstrip().rstrip("\r\n") and bt.find("=") >= 0 else 2
                # Setext: second line determines level.
                rest = bt[first_nl + 1:] if first_nl >= 0 else ""
                level = 1 if rest.lstrip().startswith("=") else 2
            units.append(("heading", f"{level}\x03{title}", [block]))
            i += 1
        elif block.kind == "fenced-code":
            m = _FENCE_RE.search(bt)
            if m:
                units.append(("fence", f"{m.group('code')}\x03{m.group('lang')}\x03{m.group('indent')}", [block]))
            else:
                units.append(("para", _process_para_lines(bt), [block]))
            i += 1
        elif block.kind == "indented-code":
            # Render as <pre><code> — NOT as a space-joined paragraph.
            # Strip 4-space or 1-tab indent from each non-trailing-blank line.
            code_lines: list[str] = []
            raw_lines = bt.split("\n")
            last_nonblank = max((j for j, rl in enumerate(raw_lines) if rl.strip()), default=-1)
            for j, raw_line in enumerate(raw_lines):
                line = raw_line.rstrip("\r")
                if j > last_nonblank:
                    break
                if line.startswith("    "):
                    code_lines.append(line[4:])
                elif line.startswith("\t"):
                    code_lines.append(line[1:])
                else:
                    code_lines.append(line)
            code = "\n".join(code_lines)
            units.append(("fence", f"{code}\x03\x03", [block]))
            i += 1
        elif block.kind == "html":
            # Escape raw HTML preserving newlines (NOT space-joined like para).
            # The browser textContent of <p>escaped_html_with_newlines</p>
            # preserves newlines, matching the IR's _html_block_content.
            raw = bt.rstrip("\r\n")
            units.append(("html-block", raw, [block]))
            i += 1
        elif block.kind == "paragraph":
            stripped = bt.strip()
            # Thematic break → hr.
            if _HR_RE.match(stripped):
                units.append(("hr", "", [block]))
            elif _FOOTNOTE_DEF_RE.match(stripped):
                m_fn = _FOOTNOTE_DEF_RE.match(stripped)
                units.append(("footnote", f"{m_fn.group('id')}\x03{m_fn.group('text')}", [block]))
            else:
                units.append(("para", _process_para_lines(bt), [block]))
            i += 1
        elif block.kind == "block-quote":
            quote_lines: list[str] = []
            for raw in bt.split("\n"):
                line = raw.rstrip("\r")
                if not line.strip():
                    continue
                if line.lstrip().startswith(">"):
                    quote_lines.append(re.sub(r"^\s*>\s?", "", line))
                else:
                    quote_lines.append(line.strip())
            units.append(("quote", "\n".join(quote_lines), [block]))
            i += 1
        elif block.kind == "list-item":
            group: list[MarkdownBlock] = []
            while i < len(ir) and ir[i].kind == "list-item":
                group.append(ir[i])
                i += 1
            payload = "\n".join(b.text.rstrip("\r\n") for b in group)
            units.append(("list", payload, list(group)))
        elif block.kind == "table-row":
            group2: list[MarkdownBlock] = []
            while (i < len(ir) and ir[i].kind == "table-row"
                   and (not group2 or group2[-1].end == ir[i].start)):
                group2.append(ir[i])
                i += 1
            raw_lines = [b.text.strip() for b in group2]
            if len(raw_lines) >= 2 and _TABLE_SEP_RE.match(raw_lines[1]):
                header = raw_lines[0]
                sep = raw_lines[1]
                rows = raw_lines[2:]
                units.append(("table", f"{header}\x03{sep}\x04" + "\x04".join(rows), list(group2)))
            else:
                # Pipe prose without separator — render as paragraphs.
                for b, line in zip(group2, raw_lines):
                    units.append(("para", _process_para_lines(line), [b]))
        else:
            # Unknown kinds (hr from extractor, etc.) — skip.
            i += 1
    return units


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _record_owner(block, owner_log: list, *, table_columns: int | None = None) -> str:
    """Generate an owner nonce, record the owner record, return the attr string."""
    vis = block_visible_segments(block, table_columns=table_columns)
    nonce = generate_owner_nonce()
    owner_log.append({
        "nonce": nonce,
        "block_id": block.id,
        "kind": block.kind,
        "heading_path": block.heading_path,
        "start": block.start,
        "end": block.end,
        # None = indeterminate (mapper could not compute); stays unmappable.
        "expected_visible": vis[0] if vis is not None else None,
        "segments": vis[1] if vis is not None else [],
    })
    return f' {_PROJ_OWNER_ATTR}="{nonce}"'


def _inject_first_tag(html: str, attr: str) -> str:
    """Inject an attribute string into the first opening tag of *html*."""
    if not attr:
        return html
    idx = html.find("<")
    if idx < 0:
        return html
    end = html.find(">", idx)
    if end < 0:
        return html
    if html[end - 1:end] == "/":
        return html[:end - 1].rstrip() + attr + " />"
    return html[:end] + attr + html[end:]


def markdown_to_html(text: str, *, _depth: int = 0,
                     owner_log: list | None = None) -> str:
    """Render the OKF markdown subset to HTML.

    Pure text→HTML. Links are emitted verbatim and tagged with
    ``class="okf-internal"`` / ``class="okf-external"`` for downstream
    rewriting via :func:`rewrite_internal_links`.

    If *owner_log* is a list, each projectable block's containing element is
    tagged with an opaque ``data-okf-proj-owner`` nonce and a record is
    appended. The caller (projection finalizer) verifies these survive the
    plugin unchanged.

    P1-1 (iter-4): blockquote rendering recurses via ``markdown_to_html``;
    a deeply nested ``> > > > …`` body (~1000 levels, 3KB) triggers
    ``RecursionError`` and crashes ``okf build``/``render`` + the
    ``/__data/graph.json`` bulk endpoint. Cap recursion at 32 levels
    (well beyond any legitimate nesting); deeper quotes are escaped
    as plain text.
    """
    if not text:
        return ""
    # Normalize CRLF.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = _split_blocks(text)

    out: list[str] = []
    # Slug counters for duplicate headings.
    seen_slugs: dict[str, int] = {}
    # Footnote definitions collected in document order; emitted as a
    # trailing <section> so refs anywhere in the body can jump to them.
    # Each entry: (fid, ftext, owner_nonce_or_None)
    footnotes: list[tuple[str, str, str | None]] = []

    for kind, payload, ir_blocks in blocks:
        if kind == "fence":
            code, lang, indent = payload.split("\x03")
            rendered = _render_fenced(code, lang, indent)
            if owner_log is not None and ir_blocks:
                rendered = _inject_first_tag(rendered, _record_owner(ir_blocks[0], owner_log))
            out.append(rendered)
        elif kind == "html-block":
            # Route through _render_inline so email/angle autolinks inside
            # raw-HTML-classified blocks are detected; _render_inline escapes
            # < > for non-autolink HTML (matching _html_block_content via
            # _inline_visible which uses the same tokenizer).
            attr = _record_owner(ir_blocks[0], owner_log) if owner_log is not None and ir_blocks else ""
            rendered = _render_inline(payload)
            out.append(f"<p{attr}>{rendered}</p>")
        elif kind == "heading":
            level_str, htext = payload.split("\x03", 1)
            level = int(level_str)
            slug = _slugify(htext)
            if slug in seen_slugs:
                seen_slugs[slug] += 1
                slug = f"{slug}-{seen_slugs[slug]}"
            else:
                seen_slugs[slug] = 0
            attr = _record_owner(ir_blocks[0], owner_log) if owner_log is not None and ir_blocks else ""
            out.append(
                f'<h{level} id="{_escape_attr(slug)}"{attr}>{_render_inline(htext)}</h{level}>'
            )
        elif kind == "hr":
            attr = _record_owner(ir_blocks[0], owner_log) if owner_log is not None and ir_blocks else ""
            out.append(f"<hr{attr} />")
        elif kind == "table":
            parts = payload.split("\x04")
            header, _, sep = parts[0].partition("\x03")
            rows = [p for p in parts[1:] if p]
            tr_nonces = None
            if owner_log is not None and ir_blocks:
                tr_nonces = []
                for bidx, b in enumerate(ir_blocks):
                    if bidx == 1:
                        continue  # separator row — no <tr>
                    tr_nonces.append(_record_owner(
                        b, owner_log, table_columns=len(_split_table_row(header))).split('"')[1])
            out.append(_render_table(header, rows, sep, tr_nonces=tr_nonces))
        elif kind == "footnote":
            fid, _, ftext = payload.partition("\x03")
            nonce = None
            if owner_log is not None and ir_blocks:
                nonce = _record_owner(ir_blocks[0], owner_log).split('"')[1]
            footnotes.append((fid, ftext, nonce))
        elif kind == "quote":
            attr = _record_owner(ir_blocks[0], owner_log) if owner_log is not None and ir_blocks else ""
            if _depth >= 32:
                out.append(f"<blockquote{attr}>{_escape(payload)}</blockquote>")
            else:
                # Recurse WITHOUT owner_log: inner blocks belong to the quote block.
                inner = markdown_to_html(payload, _depth=_depth + 1)
                out.append(f"<blockquote{attr}>{inner}</blockquote>")
        elif kind == "list":
            li_nonces = None
            if owner_log is not None and ir_blocks:
                li_nonces = [_record_owner(b, owner_log).split('"')[1] for b in ir_blocks]
            out.append(_render_list_block(payload.split("\n"), li_nonces=li_nonces))
        elif kind == "para":
            attr = _record_owner(ir_blocks[0], owner_log, table_columns=0) if owner_log is not None and ir_blocks else ""
            rendered = _render_inline(payload)
            rendered = rendered.replace("\x05 ", "<br />").replace("\x05", "")
            out.append(f"<p{attr}>{rendered}</p>")

    if footnotes:
        items: list[str] = []
        for fid, ftext, nonce in footnotes:
            slug = _escape_attr(_footnote_slug(fid))
            owner_attr = f' {_PROJ_OWNER_ATTR}="{nonce}"' if nonce else ""
            items.append(
                f'<li id="fn-{slug}"{owner_attr}>{_render_inline(ftext)} '
                f'<a href="#fnref-{slug}" class="okf-footnote-back" '
                f'aria-label="Back to reference">↩</a></li>'
            )
        out.append(
            '<section class="okf-footnotes" role="doc-endnotes" '
            'aria-label="Footnotes"><ol>' + "".join(items) + "</ol></section>"
        )

    return "\n".join(out)


def _build_norm_pos_map(text: str) -> list[int]:
    """Map normalized-text offsets → original-text offsets.

    Normalisation replaces ``\\r\\n`` (2 chars) and ``\\r`` (1 char) with a
    single ``\\n``. This map lets us convert offsets computed on normalised
    text back to canonical source offsets.
    """
    mapping: list[int] = []
    orig = 0
    while orig < len(text):
        mapping.append(orig)
        if text[orig:orig + 2] == "\r\n":
            orig += 2
        else:
            orig += 1
    mapping.append(orig)
    return mapping


def render_blocks_with_owners(body_text: str) -> tuple[str, list[dict]]:
    """Render body markdown to HTML, tagging each projectable block's
    containing element with an opaque ``data-okf-proj-owner`` nonce.

    Returns ``(html, owners)`` where each owner record is::

        {nonce, block_id, kind, heading_path, start, end,
         expected_visible, segments}

    Offsets (``start``, ``end``, and segment ``source_start``/``source_end``)
    are in **original body-text** code-point coordinates (preserving CRLF),
    so the caller adds the body offset to get canonical source coordinates.
    """
    owners: list[dict] = []
    normalized = body_text.replace("\r\n", "\n").replace("\r", "\n")
    html = markdown_to_html(normalized, owner_log=owners)
    if "\r" not in body_text:
        return html, owners
    # Convert normalised-text offsets back to original-text offsets.
    pos_map = _build_norm_pos_map(body_text)
    for owner in owners:
        owner["start"] = pos_map[owner["start"]] if owner["start"] < len(pos_map) else owner["start"]
        owner["end"] = pos_map[owner["end"]] if owner["end"] < len(pos_map) else owner["end"]
        segments = []
        for seg in owner["segments"]:
            start, end = seg["source_start"], seg["source_end"]
            visible = owner["expected_visible"]
            # LF list text and its structural newline may coalesce as an
            # identity run. Restoring CRLF makes that newline a 2-to-1
            # transform: preserve its own boundaries instead of publishing a
            # mixed-width "one\r\n" -> "one\n" segment. Split only proven
            # identity text; never reinterpret an inline/checkbox transform.
            if (owner["kind"] == "list-item" and visible is not None
                    and normalized[start:end] == visible[seg["dom_start"]:seg["dom_end"]]):
                cuts = {start, end}
                for match in re.finditer("\n", normalized[start:end]):
                    newline = start + match.start()
                    if pos_map[newline + 1] - pos_map[newline] == 2:
                        cuts.update((newline, newline + 1))
                ordered = sorted(cuts)
                for a, b in zip(ordered, ordered[1:]):
                    segments.append({"source_start": pos_map[a], "source_end": pos_map[b],
                                     "dom_start": seg["dom_start"] + a - start,
                                     "dom_end": seg["dom_start"] + b - start})
                continue
            seg["source_start"] = pos_map[seg["source_start"]] if seg["source_start"] < len(pos_map) else seg["source_start"]
            seg["source_end"] = pos_map[seg["source_end"]] if seg["source_end"] < len(pos_map) else seg["source_end"]
            segments.append(seg)
        owner["segments"] = segments
    return html, owners


def rewrite_internal_links(
    html: str, link_map: Mapping[str, str | None]
) -> str:
    """Rewrite internal ``<a href>`` URLs using a target_raw → url map.

    ``link_map`` maps each ``target_raw`` (the exact link as written in
    markdown, e.g. ``/a/b.md`` or ``./c.md``) to its viewer URL (or ``None``
    if the link is unresolved/broken — the href is left intact and a
    ``data-okf-broken`` attribute is added).

    Anchor-only and external links are passed through untouched. The
    markdown renderer already added ``class="okf-internal"`` to candidate
    internal links, but this function decides the final href purely from
    ``link_map``.
    """
    def _replace(m: re.Match[str]) -> str:
        pre = m.group(1)
        quote = m.group(2)
        href = m.group("href")
        # Strip an optional anchor for lookup.
        anchor = ""
        lookup = href
        if "#" in href:
            lookup, anchor = href.split("#", 1)
            anchor = f"#{anchor}"
        if lookup in link_map:
            url = link_map[lookup]
            if url is None:
                # Unresolved internal link — flag it but keep href readable.
                return f'{pre}href={quote}{href}{quote} data-okf-broken="1"'
            new_href = url + anchor
            return f'{pre}href={quote}{new_href}{quote}'
        return m.group(0)

    return _LINK_HREF_RE.sub(_replace, html)


# ---------------------------------------------------------------------------
# URL helpers (per build mode)
# ---------------------------------------------------------------------------

def url_for_concept(
    cid: ConceptId,
    mode: str,
    *,
    source_cid: ConceptId | None = None,
) -> str:
    """Return the URL for a concept under the given build mode.

    Modes:
        serve, spa → ``/<concept_id>`` (absolute, no extension)
        static     → relative path from ``source_cid``'s file to the target's
                     ``.html`` file (so the site works without a server)
    """
    cid_str = concept_id_to_str(cid)
    if mode in ("serve", "spa"):
        return "/" + cid_str
    # static
    target_rel = "/".join(cid[:-1] + (cid[-1] + ".html",))
    if source_cid is None or len(source_cid) <= 1:
        return target_rel
    source_dir = "/".join(source_cid[:-1])
    return posixpath.relpath(target_rel, source_dir) if source_dir else target_rel
