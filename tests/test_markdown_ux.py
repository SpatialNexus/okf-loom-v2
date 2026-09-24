"""Markdown feature-completeness tests for the viewer renderer (UX pass).

Covers the GFM-adjacent features added so the studio renders bundle prose
"the best way possible": table column alignment + escaped pipes + row
normalisation, task lists, strikethrough, autolinks, hard line breaks,
single-line footnotes — plus the two long-standing inline bugs fixed in the
same pass (images eaten by the link regex; ``&`` double-escaping in link
labels/hrefs). Client-side behaviour (sorting/filtering/resizing etc.) is
exercised by the browser proofs; these tests pin the server-side HTML
contract that the client code builds on.
"""
from __future__ import annotations

from okf_loom.viewer.markdown import markdown_to_html


# ---------------------------------------------------------------------------
# Tables: alignment, escaped pipes, row normalisation
# ---------------------------------------------------------------------------


def test_table_alignment_classes_from_separator() -> None:
    html = markdown_to_html(
        "| L | C | R |\n|:--|:-:|--:|\n| a | b | c |"
    )
    assert "<th>L</th>" in html                       # left = default, no class
    assert '<th class="okf-al-c">C</th>' in html
    assert '<th class="okf-al-r">R</th>' in html
    assert '<td class="okf-al-c">b</td>' in html
    assert '<td class="okf-al-r">c</td>' in html


def test_table_escaped_pipe_is_literal_cell_content() -> None:
    html = markdown_to_html("| K |\n|---|-|\n| a \\| b | x |")
    assert "<td>a | b</td>" in html


def test_table_rows_padded_and_truncated_to_header_width() -> None:
    html = markdown_to_html(
        "| A | B |\n|---|---|\n| only |\n| x | y | extra |"
    )
    # Short row padded with an empty cell; long row truncated to 2 columns.
    assert "<tr><td>only</td><td></td></tr>" in html
    assert "extra" not in html


def test_table_single_dash_separator_accepted() -> None:
    html = markdown_to_html("| A | B |\n|-|-|\n| 1 | 2 |")
    assert '<table class="okf-table">' in html


# ---------------------------------------------------------------------------
# Task lists
# ---------------------------------------------------------------------------


def test_task_list_checkboxes() -> None:
    html = markdown_to_html("- [ ] open\n- [x] done\n- plain")
    assert html.count('<li class="okf-task">') == 2
    assert html.count('class="okf-task__box" disabled=""') == 2
    assert html.count('checked=""') == 1
    assert "<li>plain</li>" in html
    # The [x] marker itself must not leak into the visible text.
    assert "[x]" not in html and "[ ]" not in html


# ---------------------------------------------------------------------------
# Inline: strikethrough, autolinks, hard breaks
# ---------------------------------------------------------------------------


def test_strikethrough() -> None:
    assert "<del>gone</del>" in markdown_to_html("a ~~gone~~ b")


def test_strikethrough_not_across_spaces() -> None:
    # ~~ with immediate whitespace is not a strike open per GFM.
    assert "<del>" not in markdown_to_html("a ~~ not struck ~~ b")


def test_angle_autolink() -> None:
    html = markdown_to_html("see <https://ex.com/a?x=1&y=2>")
    assert 'href="https://ex.com/a?x=1&amp;y=2"' in html
    assert 'class="okf-external"' in html


def test_bare_url_autolink_trims_trailing_punctuation() -> None:
    html = markdown_to_html("go to https://ex.com/path.")
    assert 'href="https://ex.com/path"' in html
    assert ">https://ex.com/path</a>." in html


def test_email_autolink() -> None:
    assert 'href="mailto:bob@example.com"' in markdown_to_html("<bob@example.com>")


def test_autolink_does_not_fire_inside_markdown_links() -> None:
    html = markdown_to_html("[label](https://ex.com/x)")
    # Exactly one anchor: the markdown link, not a nested autolink.
    assert html.count("<a ") == 1
    assert ">label</a>" in html


def test_autolink_blocks_unsafe_schemes() -> None:
    html = markdown_to_html("<javascript:alert(1)> stays text")
    assert "<a " not in html
    assert "javascript:alert" in html  # escaped, inert text


def test_hard_break_trailing_spaces_and_backslash() -> None:
    html = markdown_to_html("one  \ntwo\\\nthree")
    assert html.count("<br />") == 2
    assert "three</p>" in html


def test_no_hard_break_on_plain_wrap() -> None:
    assert "<br" not in markdown_to_html("one\ntwo")


# ---------------------------------------------------------------------------
# Footnotes
# ---------------------------------------------------------------------------


def test_footnotes_refs_and_section() -> None:
    html = markdown_to_html(
        "Claim.[^1]\n\n[^1]: The evidence with [a link](https://e.com)."
    )
    assert '<sup class="okf-footnote-ref" id="fnref-1"><a href="#fn-1">[1]</a></sup>' in html
    assert '<section class="okf-footnotes"' in html
    assert '<li id="fn-1">' in html
    assert 'class="okf-footnote-back"' in html
    # Footnote text is inline-rendered.
    assert 'href="https://e.com"' in html


def test_footnote_id_sanitised_for_html() -> None:
    html = markdown_to_html('Ref[^a"b]\n\n[^a"b]: def')
    assert 'id="fn-a-b"' in html
    assert '"b"' not in html.split("okf-footnotes")[-1].split("id=")[1][:12]


# ---------------------------------------------------------------------------
# Inline regressions fixed in the same pass
# ---------------------------------------------------------------------------


def test_images_render_as_img_not_eaten_by_link_regex() -> None:
    html = markdown_to_html("look ![a cat](./cat.png) here")
    assert '<img alt="a cat" src="./cat.png"' in html
    assert "!<a" not in html


def test_image_unsafe_src_blocked() -> None:
    html = markdown_to_html("![x](javascript:alert(1))")
    assert "<img" not in html
    assert "okf-img-blocked" in html


def test_link_label_and_href_ampersands_not_double_escaped() -> None:
    html = markdown_to_html("[A & B](https://x.example/?a=1&b=2)")
    assert ">A &amp; B</a>" in html
    assert 'href="https://x.example/?a=1&amp;b=2"' in html
    assert "&amp;amp;" not in html


def test_entity_encoded_scheme_still_not_executable() -> None:
    # &#106;avascript resolves (after ONE html-attr decode in the browser)
    # to a harmless relative URL, never to a javascript: scheme.
    html = markdown_to_html("[x](&#106;avascript:alert(1))")
    assert 'href="javascript' not in html.lower().replace("&amp;", "&")
