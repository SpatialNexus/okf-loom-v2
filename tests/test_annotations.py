import copy
import hashlib
import itertools

import pytest

from scripts.okf_loom.annotations import (
    AnchorError, capture_anchor, concept_content_rev, extract_blocks,
    parse_v2_anchor, resolve_anchor, _Candidate, _intersect_supports,
)


def captured(source="## A\n\nBefore target after\n"):
    start = source.index("target")
    return source, capture_anchor(source, start, start + 6)


def test_capture_schema_blocks_and_original_resolution():
    source, anchor = captured()
    assert anchor == {
        "schema": 2, "kind": "text-quote", "exact": "target",
        "prefix": "Before ", "suffix": " after\n", "heading": {"path": ["A"]},
        "block": {"id": "paragraph:2", "hash": anchor["block"]["hash"], "start": 6, "end": 26},
        "position": {"start": 13, "end": 19},
    }
    assert resolve_anchor(anchor, source)["reason"] == "original_position"
    assert concept_content_rev(source) == concept_content_rev(source)


@pytest.mark.parametrize("mutation,reason", [
    (lambda a: {**a, "schema": 3}, "unsupported_schema"),
    (lambda a: {**a, "extra": 1}, "malformed_anchor"),
    (lambda a: {k: v for k, v in a.items() if k != "suffix"}, "malformed_anchor"),
    (lambda a: {**a, "kind": "TEXT-QUOTE"}, "malformed_anchor"),
    (lambda a: {**a, "exact": ""}, "malformed_anchor"),
    (lambda a: {**a, "prefix": "x" * 257}, "oversized_anchor"),
    (lambda a: {**a, "position": {"start": True, "end": 19}}, "invalid_offsets"),
    (lambda a: {**a, "position": {"start": 19, "end": 13}}, "invalid_offsets"),
    (lambda a: {**a, "heading": {"path": [""]}}, "malformed_anchor"),
    (lambda a: {**a, "block": {**a["block"], "hash": "bad"}}, "malformed_anchor"),
])
def test_closed_public_schema_invalid_battery(mutation, reason):
    source, anchor = captured()
    bad = mutation(copy.deepcopy(anchor))
    assert resolve_anchor(bad, source, public=True)["reason"] == reason


def test_stored_schema_is_open_and_input_is_not_rewritten():
    source, anchor = captured(); anchor["future"] = {"keep": True}; before = copy.deepcopy(anchor)
    assert resolve_anchor(anchor, source)["status"] == "resolved"
    assert anchor == before
    with pytest.raises(AnchorError): parse_v2_anchor(anchor, public=True)


def test_insertions_movement_heading_rename_and_removal():
    source, anchor = captured()
    before = "intro\n\n" + source
    moved = "## B\n\nElse\n\nBefore target after\n"
    renamed = source.replace("## A", "## Renamed")
    assert resolve_anchor(anchor, before)["status"] == "resolved"
    assert resolve_anchor(anchor, moved)["status"] == "resolved"
    assert resolve_anchor(anchor, renamed)["status"] == "resolved"
    assert resolve_anchor(anchor, before)["changes"][:2] == ["position_moved", "block_moved"]
    assert resolve_anchor(anchor, moved)["reason"] == "unique_context"
    assert resolve_anchor(anchor, renamed)["changes"] == ["position_moved", "block_moved", "heading_changed"]
    assert resolve_anchor(anchor, source.replace("target", "gone"))["reason"] == "exact_not_found"


def test_duplicates_never_first_match_and_heading_disambiguates():
    source = "## A\n\ntarget\n\n## B\n\ntarget\n"
    second = source.rindex("target")
    anchor = capture_anchor(source, second, second + 6)
    assert resolve_anchor(anchor, source)["position"]["start"] == second
    contextless = copy.deepcopy(anchor)
    contextless["heading"]["path"] = ["missing"]
    contextless["block"].update(id="paragraph:99", hash="sha256:" + "0" * 64, start=100, end=106)
    contextless["position"] = {"start": 100, "end": 106}
    contextless["prefix"] = "x"; contextless["suffix"] = "y"
    assert resolve_anchor(contextless, source)["reason"] == "insufficient_context"


def test_overlapping_occurrences_and_conflicting_evidence():
    source = "aaaa"
    anchor = capture_anchor(source, 0, 3)
    anchor["block"].update(id="paragraph:99", hash="sha256:" + "0" * 64, start=10, end=13)
    anchor["position"] = {"start": 10, "end": 13}
    anchor["prefix"] = "x"; anchor["suffix"] = "y"
    # All three overlapping occurrences are enumerated; none is chosen first.
    overlap = resolve_anchor(anchor, source)
    assert (overlap["reason"], overlap["candidate_count"]) == ("multiple_exact_candidates", 2)
    source2 = "## A\n\ntarget\n\n## B\n\ntarget\n"
    a = capture_anchor(source2, source2.index("target"), source2.index("target") + 6)
    a["block"]["start"] = 0; a["block"]["end"] = len(source2)
    a["position"] = {"start": source2.rindex("target"), "end": source2.rindex("target") + 6}
    assert resolve_anchor(a, source2)["reason"] == "conflicting_evidence"


def test_legacy_is_unique_only_and_malformed_is_invalid():
    assert resolve_anchor({"kind": "text", "ref": "x"}, "x")["reason"] == "unique_global_exact"
    assert resolve_anchor({"kind": "text", "ref": "x"}, "xx")["reason"] == "multiple_exact_candidates"
    assert resolve_anchor({"kind": "text", "ref": "x"}, "")["reason"] == "exact_not_found"
    assert resolve_anchor({"kind": "text", "ref": ""}, "x")["reason"] == "malformed_anchor"
    assert resolve_anchor({"kind": "text", "ref": "x", "future": 1}, "x", public=True)["reason"] == "malformed_anchor"
    # Stored (non-public) legacy anchors with unknown fields are readable per spec §12.1.
    assert resolve_anchor({"kind": "text", "ref": "x", "future": 1}, "x")["status"] == "resolved"


def test_public_legacy_anchor_enforces_exact_bound_and_stops_after_ambiguity(monkeypatch):
    import scripts.okf_loom.annotations as module
    assert resolve_anchor({"kind": "text", "ref": "x" * (module.MAX_EXACT + 1)}, "x",
                          public=True)["reason"] == "oversized_anchor"
    observed = []
    def occurrences(_source, _exact):
        for index in range(100):
            observed.append(index)
            yield index
    monkeypatch.setattr(module, "_occurrences", occurrences)
    result = resolve_anchor({"kind": "text", "ref": "x"}, "xxx", public=True)
    assert (result["status"], result["candidate_count"]) == ("ambiguous", 2)
    assert observed == [0, 1]


def test_unicode_combining_crlf_no_normalization_and_code_point_offsets():
    source = "# Hé\r\n\r\n😀 e\u0301 & **x**\r\n"
    start = source.index("😀")
    anchor = capture_anchor(source, start, start + len("😀 e\u0301"))
    assert anchor["exact"] == "😀 e\u0301"
    assert anchor["position"] == {"start": start, "end": start + 4}
    assert resolve_anchor(anchor, source)["status"] == "resolved"
    assert resolve_anchor(anchor, source.replace("e\u0301", "é"))["status"] == "stale"


def test_extractor_is_deterministic_and_covers_kinds_split_merge():
    source = "# H\n\npara\nline\n\n- item\n\n> quote\n\n```\ncode\n```\n\n| a |\n\n<div>x</div>\n"
    one, two = extract_blocks(source), extract_blocks(source)
    assert one == two
    assert [b.kind for b in one] == ["heading", "paragraph", "list-item", "block-quote", "fenced-code", "table-row", "html"]
    old = capture_anchor(source, source.index("line"), source.index("line") + 4)
    split = source.replace("para\nline", "para\n\nline")
    assert resolve_anchor(old, split)["status"] == "resolved"


def test_list_item_includes_continuation_and_nested_content_not_next_sibling():
    source = "- first line\n  continuation target\n  - nested child\n    nested detail\n- second sibling\n"
    blocks = extract_blocks(source)
    first_text = "- first line\n  continuation target\n  - nested child\n    nested detail\n"
    assert [(b.kind, b.start, b.end, b.text) for b in blocks] == [
        ("list-item", 0, len(first_text), first_text),
        ("list-item", len(first_text), len(source), "- second sibling\n"),
    ]
    assert blocks[0].hash == "sha256:" + hashlib.sha256(first_text.encode()).hexdigest()
    start = source.index("target")
    anchor = capture_anchor(source, start, start + len("target"))
    assert anchor["block"] == blocks[0].public()
    assert anchor["prefix"] == first_text[: start]
    assert anchor["suffix"] == first_text[start + len("target") :]


def test_adjacent_paragraph_quote_and_html_are_separate_exact_blocks():
    source = "paragraph target\n> quote now\n<div>html now</div>\n"
    blocks = extract_blocks(source)
    expected = ["paragraph target\n", "> quote now\n", "<div>html now</div>\n"]
    cursor = 0
    for block, kind, text in zip(blocks, ("paragraph", "block-quote", "html"), expected):
        assert (block.kind, block.start, block.end, block.text) == (kind, cursor, cursor + len(text), text)
        assert block.hash == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        cursor += len(text)
    start = source.index("target")
    assert capture_anchor(source, start, start + 6)["block"] == blocks[0].public()
    quote_start = source.index("quote")
    assert capture_anchor(source, quote_start, quote_start + 5)["block"] == blocks[1].public()
    html_start = source.index("html")
    assert capture_anchor(source, html_start, html_start + 4)["block"] == blocks[2].public()


def test_capture_mapping_failure_and_revision_qualified_results():
    with pytest.raises(AnchorError): capture_anchor("a\n\nb", 0, 4)
    result = resolve_anchor(None, "😀")
    assert result["status"] == "invalid"
    assert result["concept_content_rev"] == concept_content_rev("😀")
    assert result["resolver_version"] == 2
    assert concept_content_rev("😀") != concept_content_rev("😀!")
    assert resolve_anchor(None, None)["reason"] == "source_mapping_failed"


def test_empty_context_only_supports_block_edges_and_edge_duplicates():
    source = "x\n\nx x"
    first = capture_anchor(source, 0, 1)
    assert first["prefix"] == "" and first["suffix"] == "\n"
    result = resolve_anchor(first, source)
    assert result["position"] == {"start": 0, "end": 1}
    last_start = source.rindex("x")
    last = capture_anchor(source, last_start, last_start + 1)
    assert last["suffix"] == ""
    # Empty suffix supports only the occurrence at the current block edge.
    assert resolve_anchor(last, source)["position"] == {"start": last_start, "end": last_start + 1}


def test_cross_boundary_exact_is_no_compatible_candidate():
    source = "left\n\nright\n"
    anchor = capture_anchor("left\n\nright", 0, 4)
    anchor["exact"] = "left\n\nright"
    anchor["block"]["end"] = len(anchor["exact"])
    anchor["position"] = {"start": 0, "end": len(anchor["exact"])}
    anchor["prefix"] = anchor["suffix"] = ""
    assert resolve_anchor(anchor, source)["reason"] == "no_compatible_candidate"


def test_support_intersection_is_commutative():
    source = "## A\n\ntarget\n\n## B\n\ntarget\n"
    start = source.rindex("target")
    anchor = capture_anchor(source, start, start + 6)
    baseline = resolve_anchor(anchor, source)
    original = dict
    # Construction order is irrelevant because resolution intersects all support sets.
    for order in (list(reversed(list(anchor))), sorted(anchor)):
        permuted = original((key, anchor[key]) for key in order)
        assert resolve_anchor(permuted, source) == baseline
    blocks = extract_blocks(source)
    candidates = [_Candidate(source.index("target"), source.index("target") + 6, blocks[1]), _Candidate(start, start + 6, blocks[3])]
    supports = {"a": set(candidates), "b": {candidates[1]}, "empty": set()}
    outcomes = {frozenset(_intersect_supports(candidates, supports, order)[0]) for order in itertools.permutations(supports)}
    assert outcomes == {frozenset({candidates[1]})}


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_extractor_v2_exact_fixture_matrix(newline):
    parts = [
        "    alpha", "", "    omega", "", "",
        "<section>", "inside", "</section>",
        "name | value", "--- | ---", "a | b",
        "| c | d |",
    ]
    source = newline.join(parts) + newline
    blocks = extract_blocks(source)
    code_text = newline.join(parts[:3]) + newline
    html_text = newline.join(parts[5:8]) + newline
    table_texts = [part + newline for part in parts[8:]]
    assert [block.kind for block in blocks] == ["indented-code", "html"] + ["table-row"] * 4
    assert blocks[0].text == code_text
    assert blocks[1].text == html_text
    assert [block.text for block in blocks[2:]] == table_texts
    cursor = 0
    expected_texts = [code_text, html_text] + table_texts
    # Two separator blank lines after code are deliberately not in its span.
    expected_starts = [0, source.index("<section>")] + [source.index(text.rstrip("\r\n"), source.index("name | value")) for text in table_texts]
    for block, text, start in zip(blocks, expected_texts, expected_starts):
        assert (block.start, block.end) == (start, start + len(text))
        normalized = text.replace("\r\n", "\n")
        assert block.hash == "sha256:" + hashlib.sha256(normalized.encode()).hexdigest()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_void_and_self_closing_html_never_consume_adjacent_content(newline):
    texts = [
        f"<hr>{newline}",
        f"paragraph one{newline}",
        f"<br>{newline}",
        f"<img src='x'>{newline}",
        f"<input type='text'>{newline}",
        f"<custom />{newline}",
        f"<section>{newline}inside{newline}</section>{newline}",
        f"paragraph two{newline}",
    ]
    source = "".join(texts)
    blocks = extract_blocks(source)
    assert [block.kind for block in blocks] == [
        "html", "paragraph", "html", "html", "html", "html", "html", "paragraph"
    ]
    cursor = 0
    for block, text in zip(blocks, texts):
        assert (block.start, block.end, block.text) == (cursor, cursor + len(text), text)
        normalized = text.replace("\r\n", "\n")
        assert block.hash == "sha256:" + hashlib.sha256(normalized.encode()).hexdigest()
        cursor += len(text)
    assert cursor == len(source)
    for index in (0, 2, 3, 4, 5, 6):
        selected = texts[index].index("<") + sum(map(len, texts[:index]))
        assert capture_anchor(source, selected, selected + 1)["block"] == blocks[index].public()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_heading_quote_and_special_html_grammar_with_capture_resolution(newline):
    texts = [
        f"# C#{newline}",
        f"lazy quote title{newline}---{newline}",
        f"> quoted target{newline}lazy continuation{newline}",
        f"<!-- comment{newline}continued -->{newline}",
        f"<!DOCTYPE{newline}html>{newline}",
        f"<?work{newline}done?>{newline}",
        f"<![CDATA[value{newline}done]]>{newline}",
    ]
    source = "".join(texts)
    blocks = extract_blocks(source)
    assert [block.kind for block in blocks] == ["heading", "heading", "block-quote", "html", "html", "html", "html"]
    assert blocks[0].heading_path == ("C#",)
    assert blocks[1].heading_path == ("C#", "lazy quote title")
    assert blocks[1].id == "heading:2"
    assert blocks[2].heading_path == ("C#", "lazy quote title")
    cursor = 0
    for block, text in zip(blocks, texts):
        assert (block.start, block.end, block.text) == (cursor, cursor + len(text), text)
        assert block.hash == "sha256:" + hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()
        cursor += len(text)
    selected = source.index("target")
    anchor = capture_anchor(source, selected, selected + len("target"))
    assert anchor["block"] == blocks[2].public()
    assert anchor["heading"]["path"] == ["C#", "lazy quote title"]
    assert resolve_anchor(anchor, source)["reason"] == "original_position"
    for marker in ("comment", "DOCTYPE", "work", "CDATA"):
        start = source.index(marker)
        assert capture_anchor(source, start, start + len(marker))["block"].get("id", "").startswith("html:")


def test_atx_closing_hash_requires_separating_whitespace():
    source = "# C#\n# D ###\n"
    blocks = extract_blocks(source)
    assert [block.heading_path for block in blocks] == [("C#",), ("D",)]


def test_canonical_markdown_frontmatter_and_body_selections_share_offsets():
    source = "---\ntype: note\ntitle: Canonical\n---\n\nBody target here\n"
    front_start = source.index("Canonical")
    body_start = source.index("target")
    front = capture_anchor(source, front_start, front_start + len("Canonical"))
    body = capture_anchor(source, body_start, body_start + len("target"))
    assert front["position"]["start"] == front_start
    assert body["position"]["start"] == body_start
    assert resolve_anchor(front, source)["status"] == "resolved"
    assert resolve_anchor(body, source)["status"] == "resolved"
    changed_frontmatter = source.replace("title: Canonical", "title: Changed")
    assert concept_content_rev(changed_frontmatter) != concept_content_rev(source)
