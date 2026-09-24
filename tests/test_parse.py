"""Tests for ``okf_loom.parse``.

Pinned invariants:
  * ``parse_document`` / ``serialize_document`` round-trip preserve key
    order, unknown keys, and trailing newline.
  * Missing/unterminated/malformed frontmatter raises ``OKFParseError``.
  * ``extract_links`` handles absolute (SPEC §5.1), relative (§5.2),
    external, and anchor-only links; skips links inside fenced code blocks
    and inline code spans; reports accurate line numbers.
  * ``strip_markdown_for_search`` drops code/images/HTML/headings/emphasis
    while preserving link labels.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from okf_loom.exceptions import OKFParseError
from okf_loom.parse import (
    FRONTMATTER_DELIM,
    ExtractedLink,
    extract_headings,
    extract_links,
    parse_document,
    serialize_document,
    strip_markdown_for_search,
)


# --- parse_document / serialize_document round-trip --------------------------


def test_parse_with_frontmatter() -> None:
    """Frontmatter is parsed into a dict and body is returned separately.

    Note: ``parse_document`` strips a single leading blank line that follows
    the closing ``---`` delimiter; the trailing newline of the body is NOT
    guaranteed (it is whatever the source lines joined by ``\\n`` produced).
    """
    text = "---\ntype: Table\ntitle: Hello\n---\n\nBody text here.\n"
    fm, body = parse_document(text)
    assert fm == {"type": "Table", "title": "Hello"}
    assert body.rstrip("\n") == "Body text here."


def test_parse_without_frontmatter() -> None:
    """A document without a leading ``---`` line returns ``({}, text)``."""
    text = "# Just a heading\n\nNo frontmatter here.\n"
    fm, body = parse_document(text)
    assert fm == {}
    assert body == text


def test_parse_preserves_key_order() -> None:
    """Round-trip preserves YAML key insertion order (SPEC §9 mandate)."""
    fm = {"type": "T", "zzz": 1, "aaa": 2, "mmm": [3, 4]}
    body = "Body.\n"
    s = serialize_document(fm, body)
    fm2, _ = parse_document(s)
    assert list(fm2.keys()) == ["type", "zzz", "aaa", "mmm"]


def test_parse_preserves_unknown_keys() -> None:
    """Unknown / spec-extension frontmatter keys survive a round-trip."""
    fm = {
        "type": "Table",
        "custom_namespace:key": "value",
        "another_unknown": {"nested": "dict"},
    }
    out = serialize_document(fm, "body\n")
    fm2, _ = parse_document(out)
    assert fm2["custom_namespace:key"] == "value"
    assert fm2["another_unknown"] == {"nested": "dict"}


def test_serialize_always_ends_with_single_newline() -> None:
    """Both with and without frontmatter, output ends with exactly one \\n."""
    assert serialize_document({"type": "T"}, "body").endswith("\n")
    assert serialize_document({"type": "T"}, "body\n").endswith("\n")
    # No trailing newline in body, no frontmatter:
    assert serialize_document({}, "body").endswith("body\n")


def test_serialize_omits_frontmatter_when_empty() -> None:
    """``serialize_document({}, body)`` returns just the body."""
    out = serialize_document({}, "just body\n")
    assert out == "just body\n"
    assert not out.startswith("---")


def test_round_trip_idempotent() -> None:
    """``parse(serialize(parse(x))) == parse(x)`` is a fixed point."""
    original = "---\ntype: T\ncustom: 1\n---\n\nHello world.\n"
    fm1, body1 = parse_document(original)
    once = serialize_document(fm1, body1)
    fm2, body2 = parse_document(once)
    assert fm1 == fm2
    assert body1 == body2


# --- error paths --------------------------------------------------------------


def test_unterminated_frontmatter_raises() -> None:
    """A missing closing ``---`` raises ``OKFParseError``."""
    bad = "---\ntype: T\ntitle: oops\n\nNo closing delimiter.\n"
    with pytest.raises(OKFParseError, match="Unterminated"):
        parse_document(bad)


def test_invalid_yaml_raises() -> None:
    """YAML that fails to parse surfaces as ``OKFParseError``."""
    bad = "---\nbad: yaml: with: colons\n- a\n- b\n---\n\nbody\n"
    with pytest.raises(OKFParseError, match="Invalid YAML"):
        parse_document(bad)


def test_non_mapping_yaml_raises() -> None:
    """A YAML scalar or list at the top level is rejected."""
    bad = "---\n- just\n- a\n- list\n---\n\nbody\n"
    with pytest.raises(OKFParseError, match="mapping"):
        parse_document(bad)
    bad2 = "---\njustastring\n---\n\nbody\n"
    with pytest.raises(OKFParseError, match="mapping"):
        parse_document(bad2)


def test_frontmatter_delim_constant() -> None:
    """The frontmatter delimiter is exactly three hyphens."""
    assert FRONTMATTER_DELIM == "---"


# --- extract_links: link forms -----------------------------------------------


def _links(body: str, *, source_dir: Path, bundle_root: Path) -> list[ExtractedLink]:
    return extract_links(body, source_dir=source_dir, bundle_root=bundle_root)


def test_absolute_link_form(tmp_path: Path) -> None:
    """SPEC §5.1 absolute bundle-relative link ``/a/b.md`` resolves."""
    body = "See [Users](/tables/users.md) for details.\n"
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].form == "absolute"
    assert links[0].concept_id == ("tables", "users")
    assert links[0].label == "Users"


def test_relative_links(tmp_path: Path) -> None:
    """SPEC §5.2 relative links ``./x.md``, ``../y/x.md``, ``x.md`` resolve."""
    src_dir = tmp_path / "tables"
    src_dir.mkdir()
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "tables" / "b.md").write_text("x", encoding="utf-8")
    (tmp_path / "tables" / "sub").mkdir()
    (tmp_path / "tables" / "sub" / "c.md").write_text("x", encoding="utf-8")

    body = (
        "[root](../a.md) [sibling](./b.md) [sibling2](b.md) "
        "[child](sub/c.md)\n"
    )
    links = _links(body, source_dir=src_dir, bundle_root=tmp_path)
    forms = {l.label: l.concept_id for l in links}
    assert forms["root"] == ("a",)
    assert forms["sibling"] == ("tables", "b")
    assert forms["sibling2"] == ("tables", "b")
    assert forms["child"] == ("tables", "sub", "c")
    assert all(l.form == "relative" for l in links)


def test_external_link_form(tmp_path: Path) -> None:
    """``http(s)://`` URLs classify as external with no concept_id."""
    body = (
        "[a](https://example.com) [b](http://foo.bar/x) "
        "[c](mailto:me@example.com)\n"
    )
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 3
    assert {l.form for l in links} == {"external"}
    assert all(l.concept_id is None for l in links)


def test_anchor_only_link(tmp_path: Path) -> None:
    """``#section`` is an anchor-only link.

    Classified as form=``"anchor"`` with ``concept_id=None`` and the
    fragment stored in ``anchor`` (e.g. ``"intro"`` for ``#intro``).
    """
    body = "Jump to [Intro](#intro).\n"
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].form == "anchor"
    assert links[0].concept_id is None
    assert links[0].anchor == "intro"


def test_anchor_only_link_intended(tmp_path: Path) -> None:
    """The intended behaviour: ``#intro`` should set ``anchor='intro'``."""
    body = "Jump to [Intro](#intro).\n"
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert links[0].anchor == "intro"


def test_link_with_anchor_fragment(tmp_path: Path) -> None:
    """An internal link with a ``#fragment`` resolves the concept and anchor."""
    body = "[section](/tables/users.md#schema)\n"
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert links[0].form == "absolute"
    assert links[0].concept_id == ("tables", "users")
    assert links[0].anchor == "schema"


# --- extract_links: code block / inline code skipping ------------------------


@pytest.mark.parametrize(("label", "expected"), [
    ("`/__comment-reanchor`", "/__comment-reanchor"),
    ("`/__comment-reanchor` HTTP route", "/__comment-reanchor HTTP route"),
    ("Call `route` with `args`", "Call route with args"),
    ("`[fake](/ghost.md)` example", "[fake](/ghost.md) example"),
    (r"ordinary \*label\*", r"ordinary \*label\*"),
])
def test_link_labels_preserve_inline_code_contents(tmp_path: Path, label, expected) -> None:
    links = _links(f"[{label}](/real.md#section)", source_dir=tmp_path, bundle_root=tmp_path)
    assert [(link.label, link.target_raw, link.concept_id, link.anchor) for link in links] == [
        (expected, "/real.md#section", ("real",), "section"),
    ]


def test_links_inside_fenced_code_block_skipped(tmp_path: Path) -> None:
    """``[t](x.md)`` inside ``` ``` must NOT be extracted."""
    body = (
        "Text before.\n"
        "```\n"
        "[fake](/tables/ghost.md) and [also](./nope.md)\n"
        "```\n"
        "[real](/tables/users.md)\n"
    )
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].label == "real"
    assert links[0].concept_id == ("tables", "users")


def test_links_inside_inline_code_skipped(tmp_path: Path) -> None:
    """``[t](x.md)`` inside backticks must NOT be extracted."""
    body = (
        "Use `[code](/tables/ghost.md)` for that. "
        "Real: [users](/tables/users.md).\n"
    )
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].concept_id == ("tables", "users")


def test_code_mask_preserves_link_order_lines_and_wikilink_labels(tmp_path: Path) -> None:
    body = (
        "`[fake](/ghost.md)\n[[ghost]]`\n"
        "~~~md\n[`fenced`](/ghost.md)\n~~~\n"
        "[`real`](/real.md) [[peer|`peer` label]] [ordinary](/other.md)\n"
    )
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert [(link.label, link.concept_id, link.line) for link in links] == [
        ("real", ("real",), 6), ("peer label", ("peer",), 6),
        ("ordinary", ("other",), 6),
    ]


def test_line_numbers_are_accurate(tmp_path: Path) -> None:
    """Reported ``line`` matches the 1-based source body line."""
    body = (
        "first line\n"            # 1
        "second line\n"           # 2
        "\n"                      # 3
        "[link](/a/b.md)\n"       # 4
        "more\n"                  # 5
        "[another](/c/d.md)\n"    # 6
    )
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert [l.line for l in links] == [4, 6]


def test_line_numbers_with_fenced_block_preserved(tmp_path: Path) -> None:
    """Fenced code blocks are blanked line-by-line so line numbers stay accurate."""
    body = (
        "```\n"                   # 1 fence open
        "[fake](/a/b.md)\n"       # 2 (inside code)
        "```\n"                   # 3 fence close
        "\n"                      # 4
        "[real](/c/d.md)\n"       # 5
    )
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].line == 5


def test_image_links_not_treated_as_links(tmp_path: Path) -> None:
    """``![alt](url)`` is an image, not a link — must be skipped."""
    body = "![pic](/assets/img.png) and [real](/a/b.md)\n"
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].label == "real"


# --- strip_markdown_for_search ------------------------------------------------


def test_strip_markdown_drops_code_blocks() -> None:
    """Fenced code content is removed entirely."""
    body = "hello\n```\ncode_word inside\n```\nworld\n"
    out = strip_markdown_for_search(body)
    assert "code_word" not in out
    assert "hello" in out
    assert "world" in out


def test_strip_markdown_preserves_link_label() -> None:
    """Link labels contribute their text, not the URL."""
    body = "See [Users](/tables/users.md) for details.\n"
    out = strip_markdown_for_search(body)
    assert "Users" in out
    assert "tables/users.md" not in out


def test_strip_markdown_drops_images() -> None:
    """``![alt](url)`` is dropped entirely."""
    body = "before ![alt text](https://x/y.png) after\n"
    out = strip_markdown_for_search(body)
    assert "alt text" not in out
    assert "before" in out
    assert "after" in out


def test_strip_markdown_drops_html_tags() -> None:
    """HTML tags become whitespace; tag content (if any text) is preserved."""
    body = "<div>kept</div> tail\n"
    out = strip_markdown_for_search(body)
    assert "<div>" not in out
    assert "</div>" not in out
    assert "kept" in out
    assert "tail" in out


def test_strip_markdown_drops_heading_markers() -> None:
    """Leading ``#`` heading markers are removed; heading text kept."""
    body = "# My Heading\nsome body\n"
    out = strip_markdown_for_search(body)
    assert "#" not in out
    assert "My Heading" in out


def test_strip_markdown_drops_emphasis() -> None:
    """``*``/``**``/``_``/``~~`` emphasis markers are stripped."""
    body = "*italic* **bold** _underline_ ~~strike~~\n"
    out = strip_markdown_for_search(body)
    for marker in ("*", "_", "~~"):
        assert marker not in out
    assert "italic" in out
    assert "bold" in out


def test_strip_markdown_collapses_whitespace() -> None:
    """Multiple spaces / newlines collapse to single spaces."""
    body = "a\n\n\nb     c\n\n"
    out = strip_markdown_for_search(body)
    assert "  " not in out
    assert "\n" not in out
    assert out.startswith("a")
    assert out.endswith("c")


def test_strip_markdown_empty_body() -> None:
    assert strip_markdown_for_search("") == ""
    assert strip_markdown_for_search(None) == ""  # type: ignore[arg-type]


# --- extract_headings --------------------------------------------------------


def test_extract_headings_basic() -> None:
    """Headings are returned as ``(level, slug, text)`` triples in order."""
    body = "# Title\n\n## Sub\n\ntext\n\n### Deep\n"
    hs = extract_headings(body)
    assert [(l, t) for (l, _, t) in hs] == [(1, "Title"), (2, "Sub"), (3, "Deep")]


def test_extract_headings_slug_lowercase_hyphenated() -> None:
    """Slug is lowercase, hyphen-joined, alphanumeric-only."""
    body = "# My Cool Heading!\n"
    hs = extract_headings(body)
    assert hs[0][1] == "my-cool-heading"


def test_extract_headings_disambiguates_duplicates() -> None:
    """Duplicate heading text gets a numeric suffix (CommonMark-style)."""
    body = "# Section\n# Section\n# Section\n"
    hs = extract_headings(body)
    slugs = [s for (_, s, _) in hs]
    assert slugs == ["section", "section-1", "section-2"]


def test_extract_headings_skips_inside_fenced_block() -> None:
    """``#`` inside a code fence is not a heading."""
    body = "# Real\n\n```\n# Not a heading\n```\n"
    hs = extract_headings(body)
    assert [t for (_, _, t) in hs] == ["Real"]


# --- extract_links: wikilinks (SPEC §10) -------------------------------------
#
# ``[[target]]`` and ``[[target|Label]]`` are recognised as wikilinks. The
# target is a concept id (slash-separated, no ``.md``); it resolves via the
# same concept-id resolver used for markdown links and is emitted with
# ``form="wikilink"``. Wikilinks inside fenced/inline code are skipped, and
# targets that do not parse as a concept id are flagged (concept_id=None),
# not fatal.


def test_wikilink_basic_resolves_to_concept_id(tmp_path: Path) -> None:
    """``[[tables/users]]`` resolves with form="wikilink" and the concept id.

    With no explicit label, the label defaults to the raw target string.
    """
    body = "See [[tables/users]] for details.\n"
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].form == "wikilink"
    assert links[0].concept_id == ("tables", "users")
    assert links[0].target_raw == "tables/users"
    assert links[0].label == "tables/users"
    assert links[0].anchor is None


def test_wikilink_with_label_extracts_label(tmp_path: Path) -> None:
    """``[[tables/users|Users Table]]`` extracts the explicit label."""
    body = "See [[tables/users|Users Table]] for details.\n"
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].form == "wikilink"
    assert links[0].concept_id == ("tables", "users")
    assert links[0].label == "Users Table"
    assert links[0].target_raw == "tables/users"


def test_wikilink_skipped_inside_fenced_code(tmp_path: Path) -> None:
    """Wikilinks inside fenced code blocks are not extracted (parity with
    markdown links)."""
    body = (
        "```\n"
        "[[tables/fake]]\n"
        "```\n"
        "Real: [[tables/users]]\n"
    )
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].concept_id == ("tables", "users")


def test_wikilink_invalid_target_flagged_not_fatal(tmp_path: Path) -> None:
    """A target that cannot parse as a concept id is still extracted with
    form="wikilink" but concept_id=None (permissive: flagged, not fatal)."""
    body = "Bad [[bad target!]] here.\n"
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    assert len(links) == 1
    assert links[0].form == "wikilink"
    assert links[0].concept_id is None
    assert links[0].target_raw == "bad target!"


def test_wikilink_and_markdown_links_in_document_order(tmp_path: Path) -> None:
    """Wikilinks and markdown links interleave in document order."""
    body = (
        "[md](/a/b.md) then [[x/y]] then [md2](/c/d.md) then [[z/w|L]]\n"
    )
    links = _links(body, source_dir=tmp_path, bundle_root=tmp_path)
    forms = [(l.form, l.label) for l in links]
    assert forms == [
        ("absolute", "md"),
        ("wikilink", "x/y"),
        ("absolute", "md2"),
        ("wikilink", "L"),
    ]


def test_wikilink_resolves_as_graph_edge(tmp_path: Path) -> None:
    """SPEC §10 acceptance: a fixture with wikilinks produces graph edges
    with form="wikilink"."""
    from okf_loom import Bundle

    (tmp_path / "tables").mkdir()
    (tmp_path / "references").mkdir()
    (tmp_path / "tables" / "users.md").write_text(
        "---\n"
        "type: Table\n"
        "title: Users\n"
        "---\n"
        "See [[references/metrics]] and [[tables/events|Events]].\n",
        encoding="utf-8",
    )
    (tmp_path / "references" / "metrics.md").write_text(
        "---\ntype: Reference\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "tables" / "events.md").write_text(
        "---\ntype: Table\n---\nbody\n", encoding="utf-8"
    )
    bundle = Bundle.load(tmp_path)
    graph = bundle.graph()
    wl_edges = [
        e for e in graph.edges if e.form == "wikilink" and e.source == ("tables", "users")
    ]
    targets = sorted(
        (e.target, e.label) for e in wl_edges if e.target is not None
    )
    assert targets == [
        (("references", "metrics"), "references/metrics"),
        (("tables", "events"), "Events"),
    ]
    assert all(e.form == "wikilink" for e in wl_edges)


# ===========================================================================
# P1-24: serialize_document preserves unknown keys + values but does NOT
# preserve style hints (that's serialize_document_round_trip in update.py).
# This test pins the BASELINE behaviour of serialize_document so the
# round-trip-preserving sibling in update.py has a clear contract to beat.
# ===========================================================================


def test_p1_24_serialize_document_baseline_uses_block_style() -> None:
    """P1-24 baseline: ``serialize_document`` always uses block style
    (``default_flow_style=False``). This is the BEHAVIOUR THAT MOTIVATED
    P1-24: a 1-key add via ``serialize_document`` reformats the entire
    frontmatter. The round-trip-preserving sibling
    ``okf_loom.update.serialize_document_round_trip`` exists to fix this
    while keeping ``serialize_document`` as the safe fallback.

    This test pins the baseline so regressions in either direction are
    caught: if ``serialize_document`` ever starts preserving styles, the
    round-trip helper's tests still hold; if it doesn't, the baseline
    stays explicit.
    """
    # Flow-style input.
    fm = {"type": "T", "tags": ["a", "b"]}
    out = serialize_document(fm, "body\n")
    # safe_dump default: block-style sequences.
    assert "tags:" in out
    assert "- a" in out
    assert "[a, b]" not in out


def test_p1_24_parse_round_trip_through_update_helper() -> None:
    """P1-24: a document serialized by ``update.serialize_document_round_
    trip`` parses back via ``parse_document`` with values intact.

    This guards the parse/update seam: the round-trip-preserving helper
    must always emit YAML that ``parse_document`` accepts.
    """
    from okf_loom.update import serialize_document_round_trip

    original = (
        "---\n"
        "type: Table\n"
        'title: "Hello"\n'
        "tags: [a, b]\n"
        "nested:\n"
        "  k: v\n"
        "---\n\nBody.\n"
    )
    fm, body = parse_document(original)
    fm["description"] = "added"
    out = serialize_document_round_trip(original, fm, body)
    fm2, body2 = parse_document(out)
    assert fm2 == fm
    assert body2.rstrip() == "Body."


# ---------------------------------------------------------------------------
# iter-7 P1-1: indented fenced code blocks must not leak links/headings.
# _FENCE_RE accepts 0-3 space indent (CommonMark). Mutation-proven zero
# coverage — reverting the [ ]{0,3} prefix keeps all 906 tests green.
# ---------------------------------------------------------------------------


def test_iter7_indented_backtick_fence_no_link_leak():
    """P1-1: a markdown link inside a 2-space-indented ``` fence must NOT be
    extracted as a real graph edge."""
    from okf_loom.parse import extract_links
    import tempfile
    from pathlib import Path
    tmpdir = Path(tempfile.mkdtemp())
    body = (
        "Example:\n"
        "  ```\n"
        "  [fake](/tables/ghost.md)\n"
        "  ```\n"
        "[real](/tables/users.md)\n"
    )
    links = extract_links(body, source_dir=tmpdir, bundle_root=tmpdir)
    labels = [l.label for l in links]
    assert "fake" not in labels, f"indented-fence link leaked: {labels}"
    assert "real" in labels


def test_iter7_indented_tilde_fence_no_link_leak():
    """P1-1: a markdown link inside a 2-space-indented ~~~ fence must NOT be
    extracted. Tilde fences are NOT masked by _INLINE_CODE_RE so they're the
    real risk — the backtick fence test above may pass coincidentally."""
    from okf_loom.parse import extract_links
    import tempfile
    from pathlib import Path
    tmpdir = Path(tempfile.mkdtemp())
    body = (
        "Example:\n"
        "  ~~~\n"
        "  [fake](/tables/ghost.md)\n"
        "  ~~~\n"
        "[real](/tables/users.md)\n"
    )
    links = extract_links(body, source_dir=tmpdir, bundle_root=tmpdir)
    labels = [l.label for l in links]
    assert "fake" not in labels, f"indented tilde-fence link leaked: {labels}"
    assert "real" in labels


def test_iter7_indented_fence_stripped_in_parse_log():
    """P1-1: _parse_log strips indented fenced code blocks so headings inside
    don't leak as spurious log entries."""
    from okf_loom.model import _parse_log
    raw = (
        "## 2024-01-01\n- real\n\n"
        "  ```\n  ## FAKE_INDENTED\n  - fake\n  ```\n\n"
        "## 2024-01-02\n- real2\n"
    )
    entries = _parse_log(raw)
    dates = [e.date for e in entries]
    assert "FAKE_INDENTED" not in dates, f"indented-fence heading leaked: {dates}"
    assert len(entries) == 2
