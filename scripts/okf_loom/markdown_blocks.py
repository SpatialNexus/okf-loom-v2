"""Shared canonical block IR for Annotations v2.

This module is the **single source of truth** for Markdown block structure used
by the annotation extractor (``annotations.py``), the Markdown renderer
(``viewer/markdown.py``), the projection generator (``render.py``), and tests.
It owns block grammar, source spans, heading hierarchy/hash normalisation,
hierarchy/aggregation policy, visible-text source-map segments, syntax spans,
and the renderer-owned projection marker system.

Versioning: ``CANONICAL_BLOCK_IR_VERSION = 1``. A grammar change that alters
block identities, spans, or projection policies requires a new version. The
version is exposed additively in projection metadata as ``block_ir_version``
without changing schema 1.

The block parser is intentionally NOT a full CommonMark implementation. It
implements the versioned v2 extractor grammar declared in spec §12.1: ATX/setext
headings, paragraphs (including soft-wrapped), list items (ordered ``1.`` and
``1)``, unordered ``- + *``, nested/continued), block quotes (including lazy
continuation), fenced/indented code, table rows, HTML blocks, and thematic
breaks. Line endings are preserved in source identity; the versioned block
representation normalises CRLF to LF only for hashing.

This module depends on nothing inside the package except ``html`` (stdlib) — it
must not import ``annotations`` (which would create a cycle, since
``annotations`` imports this module).
"""
from __future__ import annotations

import hashlib
import html as _html
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

CANONICAL_BLOCK_IR_VERSION = 1

# ---------------------------------------------------------------------------
# Immutable block IR
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkdownBlock:
    """One canonical projectable block emitted by the versioned grammar.

    ``id`` is ``<kind>:<one-based-source-order>``; its ordinal is evidence only.
    Spans are zero-based half-open Unicode code-point offsets into canonical
    source. ``hash`` is SHA-256 of block text after CRLF→LF only. ``text`` is
    ``source[start:end]`` (exact source characters including line endings).
    """

    id: str
    kind: str
    start: int
    end: int
    text: str
    hash: str
    heading_path: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "hash": self.hash, "start": self.start, "end": self.end}


# Backward-compat alias: annotations.Block historically pointed here.
Block = MarkdownBlock


# ---------------------------------------------------------------------------
# Grammar (spec §12.1) — the single parser used by extractor and renderer.
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^( {0,3})(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?(?:\r?\n)?$")
_SETEXT = re.compile(r"^ {0,3}(?P<marker>=+|-+)[ \t]*(?:\r?\n)?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])[ \t]+")
_ANY_LIST = re.compile(r"^(?P<indent>[ \t]*)(?:[-+*]|\d+[.)])[ \t]+")
_QUOTE = re.compile(r"^ {0,3}>")
_TABLE = re.compile(r"^\s*(?:\|.*\||[^\r\n|]+\|[^\r\n]+)\s*(?:\r?\n)?$")
_HTML = re.compile(r"^ {0,3}</?[A-Za-z][^>]*>")
_HTML_OPEN = re.compile(r"^ {0,3}<(?P<tag>[A-Za-z][A-Za-z0-9-]*)(?:\s[^>]*)?>")
_HTML_SPECIAL = (("<!--", "-->"), ("<![CDATA[", "]]>"), ("<?", "?>"), ("<!", ">"))
_HTML_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


# ---------------------------------------------------------------------------
# Depth-aware HTML tag tokenizer for paired containers.
# Counts same-name non-void non-self-closing openings and closings until
# depth returns to zero. Handles quoted attribute values containing >,
# comments/CDATA/PI/declarations, void/self-closing forms, and mixed case.
# ---------------------------------------------------------------------------

def _html_tags_on_line(line: str, target: str,
                       state: str | None = None) -> tuple[list[tuple[bool, bool]], str | None]:
    """Find all same-name opening/closing tags of *target* on one line.

    Returns ``(results, new_state)``.  ``results`` is a list of
    ``(is_closing, is_standalone)`` for each tag found.  ``is_standalone``
    is True for void tags and self-closing forms (no depth change).

    *state* carries multi-line lexical context across calls: ``"comment"``
    (inside ``<!-- -->``), ``"cdata"`` (inside ``<![CDATA[ ]]>``),
    ``"pi"`` (inside ``<? ?>``), ``"decl"`` (inside ``<! >``), or ``None``
    (normal).  When inside a multi-line construct, the entire line is
    scanned only for the construct terminator; tags inside it are ignored.
    """
    target_lower = target.lower()
    results: list[tuple[bool, bool]] = []
    n = len(line)
    i = 0

    # --- Resume from multi-line construct state ---
    if state is not None:
        terminator_map = {
            "comment": "-->",
            "cdata": "]]>",
            "pi": "?>",
            "decl": ">",
        }
        term = terminator_map.get(state)
        if term is not None:
            end = line.find(term)
            if end < 0:
                # Still inside the construct — entire line skipped.
                return [], state
            i = end + len(term)
            state = None

    # --- Normal scanning ---
    while i < n:
        if line[i] != "<":
            i += 1
            continue
        # Detect special constructs (may span to subsequent lines).
        if line.startswith("<!--", i):
            end = line.find("-->", i + 4)
            if end < 0:
                return results, "comment"
            i = end + 3
            continue
        if line.startswith("<![CDATA[", i):
            end = line.find("]]>", i + 9)
            if end < 0:
                return results, "cdata"
            i = end + 3
            continue
        if line.startswith("<?", i):
            end = line.find("?>", i + 2)
            if end < 0:
                return results, "pi"
            i = end + 2
            continue
        if line.startswith("<!", i):
            end = line.find(">", i + 2)
            if end < 0:
                return results, "decl"
            i = end + 1
            continue
        # Try to parse a regular tag.
        j = i + 1
        is_closing = False
        if j < n and line[j] == "/":
            is_closing = True
            j += 1
        if j >= n or not line[j].isalpha():
            i += 1
            continue
        tag_start = j
        while j < n and (line[j].isalnum() or line[j] == "-"):
            j += 1
        tag_name = line[tag_start:j].lower()
        if tag_name != target_lower:
            # Different tag — skip to its closing >, honouring quotes.
            in_q: str | None = None
            while j < n:
                ch = line[j]
                if in_q:
                    if ch == in_q:
                        in_q = None
                elif ch in ('"', "'"):
                    in_q = ch
                elif ch == ">":
                    break
                j += 1
            i = j + 1
            continue
        # Same-name tag — find closing >, honouring quoted attribute values.
        in_quote: str | None = None
        while j < n:
            ch = line[j]
            if in_quote:
                if ch == in_quote:
                    in_quote = None
            elif ch in ('"', "'"):
                in_quote = ch
            elif ch == ">":
                self_closing = j > tag_start and line[j - 1] == "/"
                is_void = tag_name in _HTML_VOID_TAGS
                is_standalone = self_closing or (not is_closing and is_void)
                results.append((is_closing, is_standalone))
                i = j + 1
                break
            j += 1
        else:
            i = n
    return results, state


def _find_html_block_end(lines: list, start_idx: int, tag: str) -> int:
    """Scan from *start_idx* for the depth-balanced end of a paired HTML block.

    Returns the index of the line AFTER the line where nesting depth returns
    to zero. Handles multiple opens/closes per line, nested same-name
    containers, and carries comment/CDATA/PI/declaration lexical state across
    lines so fake opens/closes inside multi-line constructs do not affect depth.
    """
    depth = 0
    state: str | None = None
    for j in range(start_idx, len(lines)):
        tags, state = _html_tags_on_line(lines[j][2], tag, state)
        for is_closing, is_standalone in tags:
            if is_standalone:
                continue
            depth += -1 if is_closing else 1
        if depth <= 0:
            return j + 1
    return len(lines)


def _lines(source: str) -> list[tuple[int, int, str]]:
    """Split source into (start, end, text) per source line, preserving endings."""
    result: list[tuple[int, int, str]] = []
    cursor = 0
    for line in source.splitlines(keepends=True):
        result.append((cursor, cursor + len(line), line))
        cursor += len(line)
    if cursor < len(source) or (not result and source):
        result.append((cursor, len(source), source[cursor:]))
    return result


def _block_hash(text: str) -> str:
    normalised = text.replace("\r\n", "\n")
    return "sha256:" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def block_hash(text: str) -> str:
    """SHA-256 of block text after CRLF→LF normalisation (spec §12.1)."""
    return _block_hash(text)


def parse_markdown_blocks(source: str) -> tuple[MarkdownBlock, ...]:
    """Extract deterministic v2 containing blocks in canonical source order.

    Walks canonical Markdown source in source order and emits ATX/setext
    headings, paragraphs, list items (including indented/nested continuation),
    block quotes (including lazy continuation), fenced/indented code, individual
    table rows, and HTML blocks. HTML blocks include paired containers,
    standalone void/self-closing tags, comments, declarations, processing
    instructions, and CDATA. Spans retain exact source characters and line
    endings, exclude trailing separating blank lines, and hashes normalise CRLF
    to LF only.

    Offsets are Python string offsets (Unicode code points).
    """
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    lines = _lines(source)
    raw: list[tuple[str, int, int, tuple[str, ...]]] = []
    headings: list[str] = []
    i = 0
    while i < len(lines):
        start, end, line = lines[i]
        if not line.strip():
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading.group(2)), heading.group(3).rstrip(" \t")
            headings[level - 1:] = [title]
            raw.append(("heading", start, end, tuple(headings)))
            i += 1
            continue
        # Fences take precedence over setext: a ```markdown fence opening
        # followed by --- (common inside code examples) must NOT be
        # misclassified as a setext heading.
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            width = len(fence.group(1))
            j = i + 1
            while j < len(lines):
                if re.match(rf"^ {{0,3}}{re.escape(marker)}{{{width},}}[ \t]*(?:\r?\n)?$", lines[j][2]):
                    j += 1
                    break
                j += 1
            raw.append(("fenced-code", start, lines[j - 1][1], tuple(headings)))
            i = j
            continue
        if i + 1 < len(lines) and line.strip() and (setext := _SETEXT.match(lines[i + 1][2])):
            title = line.rstrip("\r\n").strip()
            level = 1 if setext.group("marker")[0] == "=" else 2
            headings[level - 1:] = [title]
            raw.append(("heading", start, lines[i + 1][1], tuple(headings)))
            i += 2
            continue
        if line.startswith(("    ", "\t")):
            j = i + 1
            while j < len(lines) and (lines[j][2].startswith(("    ", "\t")) or not lines[j][2].strip()):
                j += 1
            content_end = j
            while content_end > i + 1 and not lines[content_end - 1][2].strip():
                content_end -= 1
            raw.append(("indented-code", start, lines[content_end - 1][1], tuple(headings)))
            i = j
            continue
        special_html = next(
            ((prefix, terminator) for prefix, terminator in _HTML_SPECIAL if line.lstrip().startswith(prefix)),
            None,
        )
        kind = (
            "list-item" if _LIST.match(line)
            else "block-quote" if _QUOTE.match(line)
            else "table-row" if _TABLE.match(line)
            else "html" if (_HTML.match(line) or special_html)
            else "paragraph"
        )
        j = i + 1
        if kind == "list-item":
            base_indent = len(_ANY_LIST.match(line).group("indent"))  # type: ignore[union-attr]
            while j < len(lines):
                next_line = lines[j][2]
                nested = _ANY_LIST.match(next_line)
                if nested and len(nested.group("indent")) <= base_indent:
                    break
                if not next_line.strip():
                    lookahead = j + 1
                    while lookahead < len(lines) and not lines[lookahead][2].strip():
                        lookahead += 1
                    if lookahead >= len(lines):
                        break
                    following = lines[lookahead][2]
                    following_list = _ANY_LIST.match(following)
                    leading = len(following) - len(following.lstrip(" \t"))
                    if (following_list and len(following_list.group("indent")) <= base_indent) or leading <= base_indent:
                        break
                    j = lookahead
                    continue
                leading = len(next_line) - len(next_line.lstrip(" \t"))
                if leading <= base_indent and (
                    _HEADING.match(next_line) or _FENCE.match(next_line)
                    or _QUOTE.match(next_line) or _HTML.match(next_line) or _TABLE.match(next_line)
                ):
                    break
                j += 1
        elif kind == "html":
            opening = _HTML_OPEN.match(line)
            tag = opening.group("tag") if opening else None
            if special_html and special_html[1] not in line:
                while j < len(lines):
                    current = lines[j][2]
                    j += 1
                    if special_html[1] in current:
                        break
            elif tag:
                # Depth-aware scan: nested same-name containers close at the
                # outer </tag>, not the first inner close.
                j = _find_html_block_end(lines, i, tag)
        elif kind in {"paragraph", "block-quote"}:
            while j < len(lines) and lines[j][2].strip():
                next_line = lines[j][2]
                if (
                    _HEADING.match(next_line) or _FENCE.match(next_line) or _LIST.match(next_line)
                    or (kind == "paragraph" and (_QUOTE.match(next_line) or _HTML.match(next_line)))
                    or (_TABLE.match(next_line) and kind != "html")
                ):
                    break
                if kind == "block-quote" and not _QUOTE.match(next_line) and (
                    _HEADING.match(next_line) or _FENCE.match(next_line) or _LIST.match(next_line)
                    or _HTML.match(next_line)
                    or any(next_line.lstrip().startswith(prefix) for prefix, _ in _HTML_SPECIAL)
                    or _TABLE.match(next_line)
                ):
                    break
                if kind == "html" and not _HTML.match(next_line):
                    break
                j += 1
        raw.append((kind, start, lines[j - 1][1], tuple(headings)))
        i = j
    blocks: list[MarkdownBlock] = []
    for ordinal, (kind, start, end, path) in enumerate(raw, 1):
        text = source[start:end]
        blocks.append(MarkdownBlock(f"{kind}:{ordinal}", kind, start, end, text, _block_hash(text), path))
    return tuple(blocks)


# ``extract_blocks`` is the canonical grammar entry point; it is the same
# function as ``parse_markdown_blocks``. ``annotations.extract_blocks`` is a
# thin adapter that calls this — there is no reverse dependency.
extract_blocks = parse_markdown_blocks


def projection_metadata(revision: str) -> dict[str, Any]:
    """Return additive projection metadata fields for schema-1 compatibility."""
    return {"block_ir_version": CANONICAL_BLOCK_IR_VERSION}


# ---------------------------------------------------------------------------
# Inline visibility source-mapper (deterministic, not fuzzy).
#
# Given a block's source text, computes the exact visible text a browser would
# render and a source map (segments) mapping every visible-text offset back to
# canonical source offsets. This replaces the old post-hoc fuzzy boundary
# matcher (_boundary_segments / _MD_SYNTAX_GAP_RE).
# ---------------------------------------------------------------------------

_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_STRONG_A = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRONG_U = re.compile(r"__(.+?)__", re.DOTALL)
_EM_A = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])")
_EM_U = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)_(?![\w_])")
_DEL = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~", re.DOTALL)
# Multiline-mode emphasis grammar. The HTML renderer's emphasis regexes have
# NO re.DOTALL (viewer/markdown.py ``_EMPH_RES``), so ``**``/``__``/``~~``
# cannot span a literal "\n". Logical list-item text joins continuation lines
# with real "\n" characters, so multiline tokenization must mirror that
# line-barrier exactly instead of matching delimiters across the join.
_STRONG_A_MULTILINE = re.compile(r"\*\*(.+?)\*\*")
_STRONG_U_MULTILINE = re.compile(r"__(.+?)__")
_DEL_MULTILINE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")
_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)\)")
_LINK_RE = re.compile(r"\[(?P<label>[^\]]*)\]\((?P<href>[^)\s]+)(?:\s+\"(?P<title>[^\"]*)\")?\)")
_FOOTNOTE_REF_RE = re.compile(r"\[\^(?P<id>[^\]\s]+)\]")
_AUTOLINK_ANGLE_RE = re.compile(r"<(?P<url>(?:https?|ftp)://[^\s<>]+|mailto:[^\s<>]+|tel:[^\s<>]+)>")
_AUTOLINK_EMAIL_RE = re.compile(r"<(?P<addr>[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)>")

# A token is (visible_text, source_rel_start, source_rel_end, is_identity).
# is_identity means each visible char equals the corresponding source char 1:1.
_Token = tuple[str, int, int, bool]


def find_inline_code_span(content: str, start: int = 0) -> tuple[int, int, int, int] | None:
    """Find one inline code span under the shared delimiter grammar.

    This is the SINGLE authority for inline-code delimiters, shared by the
    HTML inline renderer (:mod:`okf_loom.viewer.markdown`) and the projection
    text tokenizer (:func:`_inline_tokens`) so rendered visible text and the
    annotation projection can never disagree over mixed backtick runs (F16).

    CommonMark rule: a code span begins with a backtick string of N backticks
    and ends at the NEXT backtick string of exactly N backticks; runs that are
    longer or shorter cannot close it and stay inside the span content. If no
    equal-length closing run exists, the opener is literal text (the caller
    resumes scanning after the whole failed run).

    Returns ``(open_start, content_start, content_end, span_end)`` for the
    span found at the maximal backtick run starting at ``start``, where the
    code content is ``content[content_start:content_end]`` and the span
    consumes source ``[open_start, span_end)``; ``None`` when ``start`` is not
    a backtick or no equal-length closing run exists.
    """
    n = len(content)
    if start < 0 or start >= n or content[start] != "`":
        return None
    open_end = start + 1
    while open_end < n and content[open_end] == "`":
        open_end += 1
    run = open_end - start
    k = open_end
    while k < n:
        if content[k] == "`":
            close_end = k + 1
            while close_end < n and content[close_end] == "`":
                close_end += 1
            if close_end - k == run:
                return (start, open_end, k, close_end)
            k = close_end
        else:
            k += 1
    return None


def _inline_tokens(content: str, *, multiline: bool = False) -> list[_Token] | None:
    """Deterministically tokenize inline markdown into visible-text tokens.

    Returns a list of (visible_str, rel_start, rel_end, is_identity) tokens, or
    None if the content cannot be confidently mapped (caller fails closed).

    ``multiline=True`` is for logical text that contains real ``"\\n"``
    characters (joined list-item continuation lines). It selects the
    renderer-parity grammar for such input: emphasis delimiters cannot span a
    newline (the renderer's emphasis regexes lack DOTALL), a backslash before
    a newline stays literal visible text (the renderer only rewrites hard
    breaks for paragraphs, never inside list items), while code spans and
    link/image labels may span newlines exactly as the renderer's shared
    ``find_inline_code_span`` grammar and ``[^\\]]`` label classes allow.
    All existing single-line callers keep the unchanged default behaviour.
    """
    emphasis_patterns = (
        (_STRONG_A_MULTILINE, _STRONG_U_MULTILINE, _EM_A, _EM_U, _DEL_MULTILINE)
        if multiline else (_STRONG_A, _STRONG_U, _EM_A, _EM_U, _DEL)
    )
    tokens: list[_Token] = []
    i = 0
    n = len(content)
    while i < n:
        c = content[i]
        # 1. Inline code span: `code` or ``code`` (content visible 1:1).
        #    Shared grammar with the HTML renderer (find_inline_code_span):
        #    an opening run of N backticks closes at the next run of exactly
        #    N backticks; a longer run (e.g. the ``` inside a `` ` ```math ` ``
        #    example) cannot close a 1-backtick span.
        if c == "`":
            span = find_inline_code_span(content, i)
            if span is not None:
                _open, code_start, code_end, span_end = span
                code = content[code_start:code_end]
                if code:
                    tokens.append((code, code_start, code_end, True))
                i = span_end
                continue
            # No equal-length closing run: the whole opener run is literal.
            open_end = i + 1
            while open_end < n and content[open_end] == "`":
                open_end += 1
            tokens.append((content[i:open_end], i, open_end, True))
            i = open_end
            continue
        # 2. Character entity reference.
        if c == "&":
            m = _ENTITY_RE.match(content, i)
            if m:
                decoded = _html.unescape(m.group())
                if not decoded:
                    decoded = m.group()
                tokens.append((decoded, i, m.end(), False))
                i = m.end()
                continue
            tokens.append(("&", i, i + 1, True))
            i += 1
            continue
        # 3. Hard line break: backslash immediately before a newline. Only the
        #    paragraph pipeline rewrites hard breaks; multiline logical list
        #    text must keep the backslash visible like the renderer does.
        if not multiline and c == "\\" and i + 1 < n and content[i + 1] == "\n":
            i += 1  # skip backslash; the newline is handled by the caller's line loop
            continue
        # 4. Image: ![alt](src) — produces no visible text (img has no textContent).
        if c == "!" and i + 1 < n and content[i + 1] == "[":
            m = _IMAGE_RE.match(content, i)
            if m:
                i = m.end()
                continue
        # 5. Link [label](href) or footnote reference [^id].
        if c == "[":
            fn = _FOOTNOTE_REF_RE.match(content, i)
            if fn:
                fid = fn.group("id")
                # Visible text is "[id]" but source is "[^id]" — emit per-char
                # tokens so the "^" gap keeps them as separate identity segments.
                bracket_open = i
                id_start = i + 2  # past "[^"
                id_end = id_start + len(fid)
                bracket_close = id_end  # the "]" is right after the id
                tokens.append(("[", bracket_open, bracket_open + 1, True))
                if fid:
                    tokens.append((fid, id_start, id_end, True))
                tokens.append(("]", bracket_close, bracket_close + 1, True))
                i = fn.end()
                continue
            m = _LINK_RE.match(content, i)
            if m:
                label = m.group("label")
                label_start = m.start("label")
                sub = _inline_tokens(label, multiline=multiline)
                if sub is None:
                    return None
                for vis, s, e, ident in sub:
                    tokens.append((vis, label_start + s, label_start + e, ident))
                i = m.end()
                continue
            tokens.append(("[", i, i + 1, True))
            i += 1
            continue
        # 6. Angle autolink: <url> — visible text is the URL, < and > are syntax.
        if c == "<":
            m = _AUTOLINK_ANGLE_RE.match(content, i) or _AUTOLINK_EMAIL_RE.match(content, i)
            if m:
                inner_start = i + 1
                inner_end = m.end() - 1
                inner = content[inner_start:inner_end]
                if inner:
                    tokens.append((inner, inner_start, inner_end, True))
                i = m.end()
                continue
            tokens.append(("<", i, i + 1, True))
            i += 1
            continue
        # 7. Emphasis / strong / strikethrough delimiters.
        if c in "*_~":
            matched = False
            for pat in emphasis_patterns:
                m = pat.match(content, i)
                if m:
                    inner = m.group(1)
                    sub = _inline_tokens(inner, multiline=multiline)
                    if sub is None:
                        return None
                    inner_start = m.start(1)
                    for vis, s, e, ident in sub:
                        tokens.append((vis, inner_start + s, inner_start + e, ident))
                    i = m.end()
                    matched = True
                    break
            if matched:
                continue
            tokens.append((c, i, i + 1, True))
            i += 1
            continue
        # 8. Default: literal visible character (1:1 identity).
        tokens.append((c, i, i + 1, True))
        i += 1
    return tokens


def _coalesce(tokens: list[_Token], abs_base: int) -> list[dict[str, int]]:
    """Coalesce token list into contiguous segments with absolute source offsets.

    Adjacent identity tokens whose source ranges are contiguous merge into one
    segment. Non-identity tokens (entities, transformed chars) stay standalone.
    """
    segments: list[dict[str, int]] = []
    dom_pos = 0
    for vis_str, s, e, ident in tokens:
        if not vis_str:
            continue
        seg = {
            "dom_start": dom_pos,
            "dom_end": dom_pos + len(vis_str),
            "source_start": abs_base + s,
            "source_end": abs_base + e,
        }
        prev = segments[-1] if segments else None
        if (
            prev is not None
            and prev.get("_ident", False)
            and ident
            and prev["dom_end"] == seg["dom_start"]
            and prev["source_end"] == seg["source_start"]
            and (prev["source_end"] - prev["source_start"]) == (prev["dom_end"] - prev["dom_start"])
            and (seg["source_end"] - seg["source_start"]) == (seg["dom_end"] - seg["dom_start"])
        ):
            prev["dom_end"] = seg["dom_end"]
            prev["source_end"] = seg["source_end"]
        else:
            seg["_ident"] = ident
            segments.append(seg)
        dom_pos += len(vis_str)
    for seg in segments:
        seg.pop("_ident", None)
    return segments


def _tokens_visible(tokens: list[_Token] | None) -> str | None:
    if tokens is None:
        return None
    return "".join(t[0] for t in tokens)


def _inline_visible(content: str, abs_base: int) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for inline markdown ``content``."""
    tokens = _inline_tokens(content)
    if tokens is None:
        return None
    vis = _tokens_visible(tokens)
    segs = _coalesce(tokens, abs_base)
    return vis, segs


# ---------------------------------------------------------------------------
# Block-level visible-text + source-map (dispatches by block kind).
# ---------------------------------------------------------------------------

_THEMATIC_BREAK = re.compile(r"^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*(?:\r?\n)?$")
_LIST_MARKER_RE = re.compile(r"^([ \t]*)(?:[-+*]|\d+[.)])([ \t]+)(.*)$")
_SETEXT_UNDERLINE = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")
# GFM task-list item body: "[ ] text" / "[x] text" — the renderer replaces this
# with a disabled <input> checkbox (no textContent), so the visible text drops
# the "[x] " marker and gains a template space before the text.
_TASK_ITEM_RE = re.compile(r"^\[(?P<state>[ xX])\]\s+(?P<text>.*)$", re.DOTALL)
# Footnote definition line: [^id]: text (rendered as trailing <li> in a section).
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^(?P<id>[^\]\s]+)\]:\s+(?P<text>.+?)\s*(?:\r?\n)?$")


def _split_source_lines(text: str) -> list[tuple[int, str]]:
    """Return (abs_start_of_line, line_text_without_ending) pairs."""
    out: list[tuple[int, str]] = []
    pos = 0
    for raw_line in text.split("\n"):
        # Handle \r\n: the split on \n leaves \r at end of each line if CRLF.
        line = raw_line
        if line.endswith("\r"):
            line = line[:-1]
        out.append((pos, line))
        # Advance past line + the \n (and \r already consumed via split).
        pos += len(raw_line) + 1  # +1 for the \n that split removed
    return out


def _heading_content(block: MarkdownBlock) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for a heading block."""
    text = block.text
    # ATX: #+ title
    m = re.match(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?(?:\r?\n)?$", text)
    if m:
        title = m.group(1).rstrip(" \t")
        title_start_off = m.start(1)
        return _inline_visible(title, block.start + title_start_off)
    # Setext: first line is the title.
    first_nl = text.find("\n")
    raw_title_line = text if first_nl < 0 else text[:first_nl]
    raw_title_line = raw_title_line.rstrip("\r")
    title = raw_title_line.strip()
    # Find title start offset in block text.
    lead = len(raw_title_line) - len(raw_title_line.lstrip(" \t"))
    return _inline_visible(title, block.start + lead)


def _paragraph_content(block: MarkdownBlock) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for a paragraph, matching the renderer's
    line-strip + space-join + hard-break semantics.

    The renderer (viewer/markdown.py ``_process_para_lines``) strips each
    non-blank line, joins soft-wrapped lines with a single visible space, turns
    hard line breaks (trailing 2+ spaces or ``\\``) into ``<br />`` (no text
    content), and only THEN parses inline markup on the joined payload. To
    match that exactly, inline tokenization MUST run on the joined content so
    that emphasis/strong/strikethrough/link/entity delimiters spanning a soft
    line break (e.g. ``**patches\\nin place**``) are recognised the same way
    the renderer sees them, instead of being split into per-line literal text
    (the historical parity defect that left stray ``**`` in expected_visible
    and forced the block unmapped → ``complete=False``).

    Exact source/code-point offsets are preserved: every rendered character
    maps back to its canonical source offset, and each soft-break space maps to
    the source newline span (CRLF-aware) as a non-identity segment boundary.
    """
    lines = _split_source_lines(block.text)
    # Only non-blank lines are rendered; soft-break spaces go between them.
    nonblank = [(off, line) for off, line in lines if line.strip()]
    if not nonblank:
        return "", []

    # Build the joined render-content the inline tokenizer sees, plus a position
    # map (render-content offset -> source offset relative to the block start)
    # and a record of which render offsets are soft-break spaces.
    render_chars: list[str] = []
    pos_map: list[int] = []  # render offset -> relative source offset
    soft_break_at: dict[int, tuple[int, int]] = {}  # render offset -> (nl_rel_start, nl_rel_end)

    for li, (line_off, line) in enumerate(nonblank):
        hard_break = bool(re.search(r"(  +|\\)$", line))
        stripped = line.strip()
        if stripped.endswith("\\"):
            stripped = stripped[:-1].rstrip()
        leading = len(line) - len(line.lstrip(" \t"))
        content_rel_start = line_off + leading
        for j, ch in enumerate(stripped):
            render_chars.append(ch)
            pos_map.append(content_rel_start + j)
        # Inter-line separator: only between non-blank lines.
        if li < len(nonblank) - 1:
            nl_off = line_off + len(line)
            two = block.text[nl_off:nl_off + 2]
            if two.startswith("\r\n"):
                nl_rel = (nl_off, nl_off + 2)
            elif block.text[nl_off:nl_off + 1] in ("\n", "\r"):
                nl_rel = (nl_off, nl_off + 1)
            else:
                nl_rel = (nl_off, nl_off + 1)
            if not hard_break:
                # Soft break: the renderer joins the lines with a single
                # visible space (it survives into <p> textContent).
                soft_break_at[len(render_chars)] = nl_rel
                render_chars.append(" ")
                pos_map.append(nl_rel[0])
            # Hard break: the renderer emits <br /> (no text content); the
            # adjacent line content is concatenated with no separator, matching
            # the browser textContent after the "\x05 " -> <br /> rewrite.

    content = "".join(render_chars)
    tokens = _inline_tokens(content)
    if tokens is None:
        return None

    # Remap token offsets to source offsets. A token may span one or more
    # soft-break spaces (only possible for multi-char identity tokens such as
    # code spans that cross a line break); split such tokens at each soft-break
    # space so the newline span becomes its own non-identity segment, exactly
    # preserving the source-map boundary the old per-line tokenizer produced.
    all_tokens: list[_Token] = []
    for vis, s, e, ident in tokens:
        breaks = sorted(k for k in soft_break_at if s <= k < e)
        if not breaks:
            all_tokens.append((vis, pos_map[s], pos_map[e - 1] + 1, ident))
            continue
        cursor = s
        vis_off = 0
        for roff in breaks:
            if roff > cursor:
                sub = vis[vis_off:vis_off + (roff - cursor)]
                all_tokens.append((sub, pos_map[cursor], pos_map[roff - 1] + 1, ident))
                vis_off += roff - cursor
            nl_rel = soft_break_at[roff]
            all_tokens.append((" ", nl_rel[0], nl_rel[1], False))
            vis_off += 1
            cursor = roff + 1
        if cursor < e:
            sub = vis[vis_off:]
            all_tokens.append((sub, pos_map[cursor], pos_map[e - 1] + 1, ident))

    vis = _tokens_visible(all_tokens)
    if vis is None:
        return None
    segs = _coalesce(all_tokens, block.start)
    return vis, segs


def _list_item_content(block: MarkdownBlock) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for a list-item block (which may include nested
    items and continuation lines). Matches the renderer exactly: the renderer
    (viewer/markdown.py ``_render_list_block``) appends every non-blank
    continuation line to the CURRENT item's text after a single ``"\\n"``
    (``stack[-1]["node"]["text"] += "\\n" + line.strip()``) and then renders
    that whole logical item text through ONE inline parse
    (``_render_list_item_text(node["text"])``). Projection must therefore
    tokenize the same logical item text — not each source line separately —
    so delimiters spanning a continuation boundary (e.g. an inline code span
    opening on the marker line and closing on a continuation line, or a
    fenced-looking ``​``` ``​ run inside an item, which renders as multiline
    inline code including any language text) are recognised exactly as the
    renderer sees them.

    Exact source coordinates are preserved through an explicit position map:
    logical text offsets map back to canonical source offsets, the stripped
    continuation indentation stays an unmapped source gap, each joined
    ``"\\n"`` becomes its own source-newline segment (CRLF keeps its
    two-code-point span as a non-identity segment; never a mixed-width token),
    and tokens spanning a join newline are split at it. Between an item's text
    and a nested sub-list (and between sibling items) the rendered textContent
    contains a ``"\\n"``. GFM task-list markers ``[ ]``/``[x]`` are matched on
    the same joined text the renderer matches (``_TASK_ITEM_RE`` with DOTALL)
    and replaced with a disabled ``<input>`` checkbox, whose textContent is a
    leading template space.
    """
    lines = _split_source_lines(block.text)
    abs_base = block.start
    all_tokens: list[_Token] = []
    render_failed = False

    def _newline_span(prev_line_off: int, prev_line: str) -> tuple[int, int] | None:
        """Block-relative source span of the newline ending prev_line."""
        nl_off = prev_line_off + len(prev_line)
        if block.text[nl_off:nl_off + 2].startswith("\r\n"):
            return (nl_off, nl_off + 2)
        if block.text[nl_off:nl_off + 1] in ("\n", "\r"):
            return (nl_off, nl_off + 1)
        return None

    def _emit_newline_token(prev_line_off: int, prev_line: str) -> None:
        span = _newline_span(prev_line_off, prev_line)
        if span is not None:
            all_tokens.append(("\n", span[0], span[1], span[1] - span[0] == 1))

    def _flush_item(item: dict | None) -> None:
        """Tokenize one assembled logical item and append its tokens."""
        nonlocal render_failed
        if not item or not item["chars"]:
            return
        content = "".join(item["chars"])
        pos: list[int] = item["pos"]
        nl: dict[int, tuple[int, int]] = item["nl"]
        base = 0
        # GFM task-list: match on the joined logical text exactly like
        # _render_list_item_text does, then strip "[ ] "/"[x] ...": the
        # renderer emits "<input ... /> " so textContent gains a leading
        # template space mapped to the whole matched prefix source span.
        task_m = _TASK_ITEM_RE.match(content)
        if task_m:
            prefix_len = len(content) - len(task_m.group("text"))
            last = prefix_len - 1
            prefix_end = nl[last][1] if last in nl else pos[last] + 1
            all_tokens.append((" ", pos[0], prefix_end, False))
            base = prefix_len
        sub = _inline_tokens(content[base:], multiline=True)
        if sub is None:
            render_failed = True
            return
        for vis, s, e, ident in sub:
            s += base
            e += base
            # Split any token spanning a join newline so each newline keeps
            # its own source span (CRLF stays a 2→1 non-identity segment).
            breaks = sorted(k for k in nl if s <= k < e)
            if not breaks:
                all_tokens.append((vis, pos[s], pos[e - 1] + 1, ident))
                continue
            cursor = s
            vis_off = 0
            for roff in breaks:
                if roff > cursor:
                    all_tokens.append((
                        vis[vis_off:vis_off + (roff - cursor)],
                        pos[cursor], pos[roff - 1] + 1, ident,
                    ))
                    vis_off += roff - cursor
                nl_start, nl_end = nl[roff]
                all_tokens.append(("\n", nl_start, nl_end, nl_end - nl_start == 1))
                vis_off += 1
                cursor = roff + 1
            if cursor < e:
                all_tokens.append((vis[vis_off:], pos[cursor], pos[e - 1] + 1, ident))

    # Assemble logical items line by line, exactly mirroring the renderer:
    # a marker line starts an item; each non-blank continuation line is
    # appended to the current (most recent) item after a "\n"; blank lines
    # contribute nothing.
    cur: dict | None = None
    seen_item = False
    for li, (line_off, line) in enumerate(lines):
        m = _LIST_MARKER_RE.match(line)
        if m:
            if cur is not None:
                _flush_item(cur)
                cur = None
            if seen_item:
                prev_line_off, prev_line = lines[li - 1]
                _emit_newline_token(prev_line_off, prev_line)
            indent_str, item_text = m.group(1), m.group(3)
            # Content starts after indent + marker token + trailing whitespace.
            marker_tok_len = len(line) - len(indent_str) - len(m.group(2)) - len(item_text)
            content_rel = line_off + len(indent_str) + marker_tok_len + len(m.group(2))
            stripped_item = item_text.rstrip()
            item_text_lead = len(item_text) - len(item_text.lstrip(" \t"))
            first_pos = content_rel + item_text_lead
            cur = {
                "chars": list(stripped_item),
                "pos": list(range(first_pos, first_pos + len(stripped_item))),
                "nl": {},
            }
            seen_item = True
            continue
        if cur is not None and line.strip():
            # Continuation line: renderer appends "\n" + line.strip() to the
            # current item's text. The join newline maps to the source newline
            # ending the immediately preceding line; the stripped indentation
            # stays an unmapped source gap.
            prev_line_off, prev_line = lines[li - 1]
            span = _newline_span(prev_line_off, prev_line)
            if span is None:
                continue
            join_off = len(cur["chars"])
            cur["chars"].append("\n")
            cur["pos"].append(span[0])
            cur["nl"][join_off] = span
            stripped = line.strip()
            leading = len(line) - len(line.lstrip(" \t"))
            content_rel = line_off + leading
            cur["chars"].extend(stripped)
            cur["pos"].extend(range(content_rel, content_rel + len(stripped)))
    _flush_item(cur)
    if render_failed:
        return None
    vis = _tokens_visible(all_tokens)
    if vis is None:
        return None
    segs = _coalesce(all_tokens, abs_base)
    return vis, segs


def _remap_quote_segments(
    segs: list[dict[str, int]], remap_fn,
) -> list[dict[str, int]]:
    """Remap source offsets in segments through the quote position map."""
    return [
        {
            "dom_start": s["dom_start"],
            "dom_end": s["dom_end"],
            "source_start": remap_fn(s["source_start"]),
            "source_end": remap_fn(s["source_end"]),
        }
        for s in segs
    ]


def _split_quote_list_segments(
    segs: list[dict[str, int]],
    marker_spans: list[tuple[int, int]],
) -> list[dict[str, int]] | None:
    """Split quote-interior list-item segments at structural quote-line
    markers so every published segment stays decodable by the narrow client
    decoder.

    An inner list item's identity run can coalesce across a continuation
    newline; inside a block quote the endpoint remap then stretches that one
    segment across the raw ``"\\n> "`` structural marker, so its source slice
    no longer corresponds 1:1 (or by any narrow rule) to its DOM slice — the
    historical undecodable-segment shape. This splits such a segment into
    per-line identity pieces and leaves each marker region ``(newline +
    stripped ``">"`` markers) an unmapped source gap, exactly like the
    unit-join ``"\\n"`` gaps ``_concat_quote_units`` already leaves (the
    client verifier explicitly tolerates those): within-line selections stay
    exact, and a selection crossing a quote-line break inside a list item
    finds non-contiguous segments and falls back instead of guessing.

    Unlike top-level list items (``list-item:`` records decode an all-newline
    raw to one visible newline), a quoted item lives in a ``block-quote:``
    record whose join decodes to a space rule — while the rendered ``<li>``
    textContent keeps a literal newline — so a marker segment could never
    verify against the DOM; the gap is the only decodable-safe shape.

    ``marker_spans`` holds ``(newline_start, next_content_start)`` in
    canonical source coordinates, one per quoted-line boundary. Returns None
    when a segment's DOM width does not reconcile with its split pieces (fail
    closed), never a guessed mapping.
    """
    out: list[dict[str, int]] = []
    for seg in segs:
        a, b = seg["source_start"], seg["source_end"]
        # A segment starting exactly at the marker newline is split only when
        # it extends past the next line's content: ending exactly there is
        # the paragraph soft-break shape ("\\n> " -> one visible space, rule
        # 3), which is already decodable and must stay untouched.
        inside = [
            m for m in marker_spans
            if (a < m[0] and m[1] <= b) or (a == m[0] and m[1] < b)
        ]
        if not inside:
            out.append(seg)
            continue
        dom = seg["dom_start"]
        cur = a
        for marker_start, content_start in inside:
            if marker_start < cur or content_start > b:
                return None
            width = marker_start - cur
            if width:
                out.append({
                    "dom_start": dom, "dom_end": dom + width,
                    "source_start": cur, "source_end": marker_start,
                })
                dom += width
            # The visible join newline occupies exactly one DOM code point.
            dom += 1
            cur = content_start
        if cur > b:
            return None
        if cur < b:
            if seg["dom_end"] - dom != b - cur:
                return None
            out.append({
                "dom_start": dom, "dom_end": seg["dom_end"],
                "source_start": cur, "source_end": b,
            })
        elif dom != seg["dom_end"]:
            return None
    return out


def _concat_quote_units(
    unit_results: list[tuple[str, list[dict[str, int]]]],
) -> tuple[str, list[dict[str, int]]]:
    """Concatenate unit visible texts with \\n separators and adjust dom offsets.

    Mirrors the renderer's ``"\\n".join(out)`` between top-level render units:
    each unit's visible text is placed in sequence with a single ``\\n`` text
    node between successive units (matching browser textContent).
    """
    parts = [v for v, _ in unit_results]
    visible = "\n".join(parts)
    segments: list[dict[str, int]] = []
    dom_offset = 0
    for i, (vis, segs) in enumerate(unit_results):
        for seg in segs:
            segments.append({
                "dom_start": dom_offset + seg["dom_start"],
                "dom_end": dom_offset + seg["dom_end"],
                "source_start": seg["source_start"],
                "source_end": seg["source_end"],
            })
        dom_offset += len(vis)
        if i < len(unit_results) - 1:
            dom_offset += 1  # \n between units
    return visible, segments


def _quote_list_unit(
    group: list[MarkdownBlock], depth: int, remap_fn,
    marker_spans: list[tuple[int, int, int]] | None = None,
) -> tuple[str, list[dict[str, int]]] | None:
    """Combined visible text for a run of consecutive list-item blocks inside
    a blockquote, matching ``_render_list_block`` output.

    Sibling root items in the same ordered/unordered sub-run concatenate
    WITHOUT a separator (they are ``<li>`` children of the same ``<ul>/<ol>``).
    Different sub-runs (ordered↔unordered switch) get a ``\\n`` between them
    (from ``_render_list_block``'s ``"\\n".join(out_parts)``).

    ``marker_spans`` (quoted-line structural markers, see
    :func:`_split_quote_list_segments`) splits each item's remapped segments
    so identity runs crossing a quoted-line boundary stay decodable.
    """
    item_results: list[tuple[str, list[dict[str, int]], bool]] = []
    for ib in group:
        result = block_visible_segments(ib, _depth=depth)
        if result is None:
            return None
        segs = _remap_quote_segments(result[1], remap_fn)
        if marker_spans:
            segs = _split_quote_list_segments(segs, marker_spans)
            if segs is None:
                return None
        first_line = ib.text.lstrip()
        is_ordered = bool(first_line) and first_line[0].isdigit()
        item_results.append((result[0], segs, is_ordered))

    # Split by ordered/unordered sub-runs (matching _render_list_block).
    sub_groups: list[tuple[bool, list[tuple[str, list[dict[str, int]]]]]] = []
    for vis, segs, is_ord in item_results:
        if sub_groups and sub_groups[-1][0] == is_ord:
            sub_groups[-1][1].append((vis, segs))
        else:
            sub_groups.append((is_ord, [(vis, segs)]))

    sub_unit_results: list[tuple[str, list[dict[str, int]]]] = []
    for _is_ord, items in sub_groups:
        sub_vis = "".join(v for v, _ in items)
        sub_segs: list[dict[str, int]] = []
        dom_off = 0
        for vis, segs in items:
            for seg in segs:
                sub_segs.append({
                    "dom_start": dom_off + seg["dom_start"],
                    "dom_end": dom_off + seg["dom_end"],
                    "source_start": seg["source_start"],
                    "source_end": seg["source_end"],
                })
            dom_off += len(vis)
        sub_unit_results.append((sub_vis, sub_segs))

    return _concat_quote_units(sub_unit_results)


def _quote_table_unit(
    group: list[MarkdownBlock], depth: int, remap_fn,
) -> tuple[str, list[dict[str, int]]] | None:
    """Combined visible text for a run of consecutive table-row blocks inside
    a blockquote, matching ``_render_table`` output.

    If the second row is a GFM separator, it is a real table: the separator
    row is excluded and all remaining rows' cell texts concatenate without a
    separator.  Otherwise each row is pipe-prose rendered as a separate
    ``<p>`` (joined with ``\\n``).
    """
    raw_lines = [b.text.strip().rstrip("\r") for b in group]
    if len(raw_lines) >= 2 and _TABLE_SEPARATOR.match(raw_lines[1]):
        # Real table: skip separator row (index 1).
        table_blocks = [group[0]] + list(group[2:])
        row_visibles: list[str] = []
        all_segs: list[dict[str, int]] = []
        dom_off = 0
        for ib in table_blocks:
            result = block_visible_segments(ib, _depth=depth)
            if result is None:
                return None
            vis, segs = result
            for seg in segs:
                all_segs.append({
                    "dom_start": dom_off + seg["dom_start"],
                    "dom_end": dom_off + seg["dom_end"],
                    "source_start": remap_fn(seg["source_start"]),
                    "source_end": remap_fn(seg["source_end"]),
                })
            row_visibles.append(vis)
            dom_off += len(vis)
        return "".join(row_visibles), all_segs

    # Pipe prose: each row is rendered as a separate <p> by the renderer
    # (no separator → not a real table). Use _paragraph_content to preserve
    # pipe characters as literal inline text, matching _process_para_lines.
    unit_results: list[tuple[str, list[dict[str, int]]]] = []
    for ib in group:
        result = _paragraph_content(ib)
        if result is None:
            return None
        unit_results.append((result[0], _remap_quote_segments(result[1], remap_fn)))
    return _concat_quote_units(unit_results)


def _quote_content(block: MarkdownBlock, *, _depth: int = 0) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for a block-quote block.

    The renderer (viewer/markdown.py) strips ``>`` prefixes and recursively
    re-renders the inner content as full Markdown — headings, lists, thematic
    breaks, nested blockquotes, code, tables, etc. are all interpreted as
    block-level constructs, not inline paragraph text. This function mirrors
    that recursive interpretation exactly so ``expected_visible`` matches the
    browser ``textContent`` of the rendered ``<blockquote>``.

    See viewer/markdown.py ``_split_blocks`` (block-quote branch) and
    ``markdown_to_html`` (quote branch, depth-capped recursion at 32).
    """
    abs_base = block.start
    src_lines = _split_source_lines(block.text)

    # Build inner content exactly as the renderer does: normalise CRLF→LF,
    # skip blank lines, strip one ``>`` prefix per line (or .strip() for
    # lazy-continuation lines that have no ``>``).
    inner_lines: list[tuple[str, int]] = []  # (stripped_line, abs_content_start)
    for line_off, line in src_lines:
        if not line.strip():
            continue
        if line.lstrip().startswith(">"):
            m = re.match(r"^\s*>\s?", line)
            prefix_len = len(m.group(0))
            stripped = line[prefix_len:]
            content_abs = abs_base + line_off + prefix_len
        else:
            stripped = line.strip()
            lead = len(line) - len(line.lstrip(" \t"))
            content_abs = abs_base + line_off + lead
        inner_lines.append((stripped, content_abs))

    if not inner_lines:
        return "", []

    inner_text = "\n".join(ltxt for ltxt, _ in inner_lines)

    # Structural quote-line markers in canonical source coordinates: between
    # consecutive quoted lines the raw source carries the line ending plus the
    # stripped ``">"`` markers ("\\n> ", "\\r\\n>", "\\n>\\t", ...). Endpoint
    # remapping stretches identity runs across them into undecodable segments,
    # so list-item segments are split at these spans (see
    # _split_quote_list_segments).
    marker_spans: list[tuple[int, int]] = []
    for idx in range(len(inner_lines) - 1):
        ltxt, cabs = inner_lines[idx]
        nl_start = cabs + len(ltxt)
        next_content = inner_lines[idx + 1][1]
        if next_content > nl_start:
            marker_spans.append((nl_start, next_content))

    # Position map: inner_text offset → canonical source offset.
    pos_map: list[int] = []
    for idx, (ltxt, cabs) in enumerate(inner_lines):
        for j in range(len(ltxt)):
            pos_map.append(cabs + j)
        if idx < len(inner_lines) - 1:
            pos_map.append(cabs + len(ltxt))  # \n separator between lines
    pos_map.append(inner_lines[-1][1] + len(inner_lines[-1][0]))  # sentinel

    def _remap(inner_off: int) -> int:
        if 0 <= inner_off < len(pos_map):
            return pos_map[inner_off]
        return abs_base

    # Depth cap: renderer escapes inner content as plain text at depth ≥ 32.
    if _depth >= 32:
        return None  # fail closed

    inner_blocks = parse_markdown_blocks(inner_text)
    if not inner_blocks:
        return "", []

    unit_results: list[tuple[str, list[dict[str, int]]]] = []
    footnote_units: list[tuple[str, list[dict[str, int]]]] = []
    bi = 0
    while bi < len(inner_blocks):
        ib = inner_blocks[bi]

        # Footnote definitions are collected by the renderer and emitted as a
        # trailing <section> AFTER all other blocks — defer them to the end.
        if ib.kind == "paragraph" and _FOOTNOTE_DEF_RE.match(ib.text.strip()):
            result = block_visible_segments(ib, _depth=_depth + 1)
            if result is None:
                return None
            footnote_units.append(
                (result[0], _remap_quote_segments(result[1], _remap))
            )
            bi += 1
            continue

        if ib.kind == "list-item":
            group: list[MarkdownBlock] = []
            while bi < len(inner_blocks) and inner_blocks[bi].kind == "list-item":
                group.append(inner_blocks[bi])
                bi += 1
            result = _quote_list_unit(group, _depth + 1, _remap, marker_spans)
            if result is None:
                return None
            unit_results.append(result)
        elif ib.kind == "table-row":
            group2: list[MarkdownBlock] = []
            while bi < len(inner_blocks) and inner_blocks[bi].kind == "table-row":
                group2.append(inner_blocks[bi])
                bi += 1
            result = _quote_table_unit(group2, _depth + 1, _remap)
            if result is None:
                return None
            unit_results.append(result)
        else:
            result = block_visible_segments(ib, _depth=_depth + 1)
            if result is None:
                return None
            unit_results.append(
                (result[0], _remap_quote_segments(result[1], _remap))
            )
            bi += 1

    unit_results.extend(footnote_units)

    if not unit_results:
        return "", []

    return _concat_quote_units(unit_results)


def table_cell_spans(text: str) -> list[tuple[int, int]]:
    """Raw trimmed cell spans, matching the renderer's escaped-pipe grammar."""
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    if text[start:start + 1] == "|":
        start += 1
    if end > start and text[end - 1:end] == "|" and text[end - 2:end] != "\\|":
        end -= 1
    pipes = [start + m.start() for m in re.finditer(r"(?<!\\)\|", text[start:end])]
    spans = []
    for a, b in zip([start] + [p + 1 for p in pipes], pipes + [end]):
        raw = text[a:b]
        trimmed_start = a + len(raw) - len(raw.lstrip())
        spans.append((trimmed_start, trimmed_start + len(raw.strip())))
    return spans


def table_row_contexts(blocks: tuple[MarkdownBlock, ...]) -> dict[int, tuple[int, bool]]:
    """Actual table rows -> (header width, separator), without changing the IR.

    Pipe prose is not a table, and skipped blank lines interrupt a table run.
    Keys are exact source starts, never text/ordinal guesses.
    """
    contexts = {}
    i = 0
    while i < len(blocks):
        if blocks[i].kind != "table-row":
            i += 1
            continue
        start = i
        i += 1
        while (i < len(blocks) and blocks[i].kind == "table-row"
               and blocks[i - 1].end == blocks[i].start):
            i += 1
        group = blocks[start:i]
        if len(group) >= 2 and _TABLE_SEPARATOR.match(group[1].text):
            columns = len(table_cell_spans(group[0].text))
            contexts.update({b.start: (columns, j == 1) for j, b in enumerate(group)})
    return contexts


def _table_row_content(block: MarkdownBlock, columns: int | None = None) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for a table-row block. The renderer splits cells
    on unescaped pipes and renders each cell inline; textContent is the
    concatenation of cell visible text with no separator. Escaped pipes
    ``\\|`` produce a literal ``|`` in the cell text (1 visible char from 2
    source chars)."""
    text = block.text.rstrip("\r\n")
    abs_base = block.start
    all_tokens: list[_Token] = []

    # Rendering pads short rows with empty cells and truncates long rows.
    # Neither padding nor discarded cells grants source projection authority.
    for cell_start, cell_end in table_cell_spans(text)[:columns]:
        cell_raw = text[cell_start:cell_end]
        # Unescape \| → | for inline tokenization.
        cell_unescaped = cell_raw.replace("\\|", "|")
        # But _inline_tokens works on the unescaped text. We need to map
        # offsets from unescaped back to raw. Build a position map.
        unesc_to_raw: list[int] = []  # unesc_offset → raw_offset
        ri = 0
        while ri < len(cell_raw):
            if ri + 1 < len(cell_raw) and cell_raw[ri] == "\\" and cell_raw[ri + 1] == "|":
                unesc_to_raw.append(ri)
                ri += 2
            else:
                unesc_to_raw.append(ri)
                ri += 1
        unesc_to_raw.append(len(cell_raw))  # sentinel

        sub = _inline_tokens(cell_unescaped)
        if sub is None:
            return None
        for vis, s, e, ident in sub:
            # Identity runs (notably code spans) must split at each escaped
            # pipe: only that character transforms 2 source points into 1.
            # Keeping the whole run non-identity would lose exact inner bounds.
            if ident:
                for offset, char in enumerate(vis, s):
                    a, b = unesc_to_raw[offset:offset + 2]
                    all_tokens.append((char, cell_start + a, cell_start + b, b - a == 1))
            else:
                all_tokens.append((vis, cell_start + unesc_to_raw[s],
                                   cell_start + unesc_to_raw[e], False))

    vis = _tokens_visible(all_tokens)
    if vis is None:
        return None
    segs = _coalesce(all_tokens, abs_base)
    return vis, segs
def _code_content(block: MarkdownBlock) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for fenced or indented code. All content chars
    are visible 1:1. Blank lines within the code block are preserved (they are
    visible in ``<pre><code>``). No trailing newline beyond the last content."""
    text = block.text
    abs_base = block.start
    if block.kind == "fenced-code":
        # Strip fence lines.
        m = re.match(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n", text)
        start_off = m.end() if m else 0
        end_m = re.search(r"\n[ \t]*(`{3,}|~{3,})[ \t]*(?:\r?\n)?$", text)
        end_off = end_m.start() if end_m else len(text)
        code = text[start_off:end_off]
        # Strip trailing newline.
        if code.endswith("\n"):
            code = code[:-1]
            end_off -= 1
        if not code:
            return "", []
        return code, [{
            "dom_start": 0, "dom_end": len(code),
            "source_start": abs_base + start_off,
            "source_end": abs_base + end_off,
        }]
    # indented-code: strip 4-space or tab indent from each line.
    lines = _split_source_lines(text)
    all_tokens: list[_Token] = []
    # Track which lines have content (to know where to stop adding newlines).
    last_content_idx = -1
    for li, (line_off, line) in enumerate(lines):
        if line.strip():
            last_content_idx = li
    for li, (line_off, line) in enumerate(lines):
        if li > last_content_idx:
            break  # stop at trailing blank lines
        # Strip leading indent (4 spaces or 1 tab).
        if line.startswith("    "):
            content_text = line[4:]
            content_off = line_off + 4
        elif line.startswith("\t"):
            content_text = line[1:]
            content_off = line_off + 1
        elif not line.strip():
            # Blank line within code block — visible as empty line.
            content_text = ""
            content_off = line_off
        else:
            content_text = line
            content_off = line_off
        if content_text:
            all_tokens.append((content_text, content_off, content_off + len(content_text), True))
        if li < last_content_idx:
            nl_off = line_off + len(line)
            all_tokens.append(("\n", nl_off, nl_off + 1, True))
    vis = _tokens_visible(all_tokens)
    if vis is None:
        return None
    segs = _coalesce(all_tokens, abs_base)
    return vis, segs
def _html_block_content(block: MarkdownBlock) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for an HTML block. The renderer routes html
    blocks through ``_render_inline`` (so email/angle autolinks fire and
    remaining ``<>`` are escaped). The visible text must match that, so we
    use the same deterministic inline tokenizer."""
    text = block.text.rstrip("\r\n")
    return _inline_visible(text, block.start)


_THEMATIC_BREAK_RE = re.compile(r"^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$")


def _footnote_content(block: MarkdownBlock) -> tuple[str, list[dict[str, int]]] | None:
    """Visible text + segments for a footnote definition block.

    The renderer collects footnote definitions and emits them as a trailing
    ``<section><ol><li id="fn-ID">text <a ...>\u21a9</a></li></ol></section>``.
    The ``<li>`` textContent is ``"<rendered text> \u21a9"`` — the text after
    ``[^id]: `` plus a template space and the back-arrow character.

    This function computes the exact textContent: inline-visible text of the
    footnote body, then a non-source space, then the non-source ``\u21a9``.
    """
    text = block.text.strip()
    m = _FOOTNOTE_DEF_RE.match(text)
    if not m:
        return None
    ftext = m.group("text")
    # Find the source offset of ftext within the block text.
    # The block text is: [^id]: text
    ftext_src_start = block.text.find(ftext)
    if ftext_src_start < 0:
        ftext_src_start = 0
    # Tokenize the footnote body inline.
    sub = _inline_tokens(ftext)
    if sub is None:
        return None
    all_tokens: list[_Token] = []
    for vis, s, e, ident in sub:
        all_tokens.append((vis, ftext_src_start + s, ftext_src_start + e, ident))
    # Template space + back-arrow (the renderer emits "<li>…{text} <a>↩</a>")
    # are VISIBLE but have no source bytes. They stay in the visible text so
    # the finalizer's expected-vs-rendered comparison matches the DOM
    # textContent, and stay OUT of the segments: every published segment must
    # carry a non-empty source span (source_start < source_end — the client
    # validator rejects zero-width spans), and a trailing unmapped DOM region
    # is the honest representation of non-source rendered characters.
    # Selecting " ↩" therefore falls back to a page comment instead of
    # resolving to a fake source span.
    visible_tokens = list(all_tokens)
    nl_end = block.text.rstrip("\r\n").__len__()  # end of non-newline text
    visible_tokens.append((" ", nl_end, nl_end, False))
    visible_tokens.append(("\u21a9", nl_end, nl_end, False))
    abs_base = block.start
    vis = _tokens_visible(visible_tokens)
    if vis is None:
        return None
    segs = _coalesce(all_tokens, abs_base)
    return vis, segs



def block_visible_segments(
    block: MarkdownBlock, *, _depth: int = 0, table_columns: int | None = None,
) -> tuple[str, list[dict[str, int]]] | None:
    """Compute deterministic visible text + source-map segments for one block.

    Returns (visible_text, segments) or None if the block cannot be confidently
    mapped (caller fails closed → unmapped → incomplete projection if eligible).

    ``_depth`` tracks blockquote nesting for the renderer's 32-level recursion
    cap; it is irrelevant for all non-blockquote kinds.
    """
    # Thematic-break paragraphs render as <hr /> with no text content.
    if block.kind == "paragraph" and _THEMATIC_BREAK_RE.match(block.text.strip()):
        return "", []
    if block.kind == "heading":
        return _heading_content(block)
    if block.kind == "paragraph":
        # Footnote definitions are rendered as trailing <li> elements, not <p>.
        if _FOOTNOTE_DEF_RE.match(block.text.strip()):
            return _footnote_content(block)
        return _paragraph_content(block)
    if block.kind == "list-item":
        return _list_item_content(block)
    if block.kind == "block-quote":
        return _quote_content(block, _depth=_depth)
    if block.kind == "table-row":
        # The renderer knows whether this is pipe prose (0) or a real table
        # of a particular header width. A standalone block has no such context.
        if table_columns is not None:
            return (_table_row_content(block, table_columns) if table_columns
                    else _paragraph_content(block))
        # Pipe prose (not a real table row) → treat as paragraph, matching
        # _projection_tags_for_block which maps such blocks to <p>.
        lines_check = [l.strip() for l in block.text.splitlines() if l.strip()]
        if lines_check and not all(l.startswith("|") and l.endswith("|") for l in lines_check):
            return _paragraph_content(block)
        return _table_row_content(block)
    if block.kind in ("fenced-code", "indented-code"):
        return _code_content(block)
    if block.kind == "html":
        return _html_block_content(block)
    # hr and unknown kinds: no visible text.
    return "", []


# ---------------------------------------------------------------------------
# Renderer-owned projection marker system.
#
# Opaque owner capabilities assigned during IR rendering. The post-plugin
# finalizer verifies each registered owner appears exactly once with exact
# visible text; unknown/duplicate/copied/invented owners fail closed. Verified
# markers are replaced with numeric ``data-okf-projection-key`` attributes.
# ---------------------------------------------------------------------------

_MARKER_ATTR = "data-okf-proj-owner"
_FORGED_KEY_RE = re.compile(r'\s+data-okf-projection-key="([^"]*)"')


def generate_owner_nonce() -> str:
    """Generate a random 32-hex-char nonce for a renderer-owned marker."""
    return os.urandom(16).hex()


def inject_owner_marker(raw_tag: str, nonce: str) -> str:
    """Inject ``data-okf-proj-owner="<nonce>"`` into one HTML start tag."""
    if raw_tag.endswith("/>"):
        return raw_tag[:-2].rstrip() + f' {_MARKER_ATTR}="{nonce}" />'
    if raw_tag.endswith(">"):
        return raw_tag[:-1] + f' {_MARKER_ATTR}="{nonce}">'
    return raw_tag


def inject_owner_markers(html: str, owned_tag_positions: list[tuple[int, str]]) -> str:
    """Inject owner markers into HTML. *owned_tag_positions* is a list of
    ``(position, raw_tag)`` pairs in reverse order (highest first)."""
    result = html
    for pos, raw_tag in owned_tag_positions:
        nonce = generate_owner_nonce()
        result = result[:pos] + inject_owner_marker(raw_tag, nonce) + result[pos + len(raw_tag):]
    return result


# Sentinel for "attribute not present" vs "present but valueless".
_OWNER_ABSENT: Any = object()


def find_owner_markers(html: str) -> list[dict[str, Any]]:
    """Find all ``data-okf-proj-owner`` markers in HTML (document order).

    Uses **structural HTMLParser** — not regex on raw text — to find owner
    attributes on start tags. This prevents false matches from nonce-like
    strings in text content, other attribute values, or titles. Handles
    all quoting styles (double, single, unquoted, valueless) and mixed-case
    attribute names.

    Each result includes the lowercased start-tag name as ``tag`` and
    ``valueless`` flag for boolean-attribute forms.
    """
    results: list[dict[str, Any]] = []

    class _Scanner(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self._cursor = 0

        def _process(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            raw = self.get_starttag_text() or ""
            attr_map: dict[str, str | None] = {}
            for k, v in attrs:
                attr_map[k.lower()] = v
            owner_val = attr_map.get("data-okf-proj-owner", _OWNER_ABSENT)
            if owner_val is not _OWNER_ABSENT:
                pos = html.find(raw, self._cursor) if raw else -1
                if pos >= 0:
                    self._cursor = pos + len(raw)
                else:
                    pos = -1
                results.append({
                    "nonce": owner_val if owner_val is not None else "",
                    "position": pos,
                    "tag_text": raw,
                    "tag": tag,
                    "valueless": owner_val is None,
                })

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self._process(tag, attrs)

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self._process(tag, attrs)

    scanner = _Scanner()
    try:
        scanner.feed(html)
    except Exception:
        pass
    return results


def verify_owner_markers(
    pre_plugin_markers: list[str],
    post_plugin_html: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Verify owner markers after plugin execution (legacy all-required form).

    Returns ``(valid_markers, errors)``. Each pre-plugin nonce must appear
    exactly once; duplicates/missing/extra are errors.
    """
    errors: list[str] = []
    post_markers = find_owner_markers(post_plugin_html)
    counts: dict[str, int] = {}
    for pm in post_markers:
        counts[pm["nonce"]] = counts.get(pm["nonce"], 0) + 1
    for nonce in pre_plugin_markers:
        c = counts.get(nonce, 0)
        if c == 0:
            errors.append(f"owner marker removed by plugin: {nonce}")
        elif c > 1:
            errors.append(f"owner marker duplicated {c}x by plugin: {nonce}")
    pre_set = set(pre_plugin_markers)
    for pm in post_markers:
        if pm["nonce"] not in pre_set:
            errors.append(f"forged/invented owner marker: {pm['nonce']}")
    if errors:
        return [], errors
    return post_markers, []
