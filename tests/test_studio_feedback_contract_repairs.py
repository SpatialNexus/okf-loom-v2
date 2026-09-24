"""Production-seam regressions for the remaining Studio contract findings."""
import html
import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.annotations import capture_anchor, resolve_anchor
from okf_loom.render import render_concept_body_with_projection
from okf_loom.studio import Studio
from okf_loom.viewer.markdown import markdown_to_html


class Rendered(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.rows = []
        self.links = []
        self.tables = 0
        self.current = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.tables += 1
        if tag == "tr":
            self.current = {"key": attrs.get("data-okf-projection-key"), "text": "", "cells": 0}
            self.rows.append(self.current)
        if tag in {"th", "td"} and self.current is not None:
            self.current["cells"] += 1
        if tag == "a":
            self.links.append(attrs)

    def handle_endtag(self, tag):
        if tag == "tr":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data


def project(tmp_path, body, newline="\n"):
    path = tmp_path / "note.md"
    source = ("---\ntype: Note\n---\n\n" + body).replace("\n", newline)
    path.write_bytes(source.encode())
    bundle = Bundle.load(tmp_path)
    # Match the HTTP snapshot seam: Bundle.load uses universal-newline reads;
    # the live canonical reader adopts the byte-exact source before projection.
    bundle.concepts[("note",)]._adopt_raw_text(source)
    rendered, projection = render_concept_body_with_projection(
        bundle.concepts[("note",)], bundle, mode="serve")
    return source, rendered, projection


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("header,row,visible", [
    ("| A | B |", "| one | two |", "onetwo"),
    ("A | B", "one | two", "onetwo"),
    ("| A | B |", "| one | two | discarded |", "onetwo"),
    ("| A | B |", "| one |", "one"),
    ("| A | B |", "| `x\\|y😀` | &amp; |", "x|y😀&"),
    ("| A | B |", "| x\\|y | **bold** |", "x|ybold"),
    ("| A | B |", "|  |  |", ""),
])
def test_table_projection_matches_rendered_cells_and_exact_source(
        tmp_path, newline, header, row, visible):
    source, rendered, projection = project(tmp_path, header + "\n| --- | --- |\n" + row + "\n", newline)
    parsed = Rendered(rendered)
    assert projection["complete"] is True
    assert parsed.tables == 1
    assert [r["text"] for r in parsed.rows] == ["AB", visible]
    assert [r["cells"] for r in parsed.rows] == [2, 2]
    mapped = {str(r["dom_key"]): r for r in projection["blocks"] if r["dom_key"] is not None}
    for row in parsed.rows:
        if not row["text"]:
            assert row["key"] is None
            continue
        record = mapped[row["key"]]
        assert record["segments"][-1]["dom_end"] == len(row["text"])
        for seg in record["segments"]:
            raw = source[seg["source_start"]:seg["source_end"]]
            decoded = "|" if raw == "\\|" else html.unescape(raw)
            assert decoded == row["text"][seg["dom_start"]:seg["dom_end"]]
    if "discarded" in source:
        assert all(s["source_end"] <= source.index("discarded")
                   for r in mapped.values() for s in r["segments"])
    if "`x\\|y😀`" in source:
        segments = mapped[parsed.rows[1]["key"]]["segments"]
        assert [source[s["source_start"]:s["source_end"]] for s in segments] == ["x", "\\|", "y😀", "&amp;"]
        start = source.index("y😀")
        anchor = capture_anchor(source, start, start + 2)
        assert anchor["exact"] == "y😀"
        assert resolve_anchor(anchor, source)["position"] == {"start": start, "end": start + 2}


@pytest.mark.parametrize("gap", ["\n", " \n\t\n", "\r\n"])
def test_blank_lines_separate_tables_without_changing_canonical_rows(tmp_path, gap):
    body = "| A | B |\n| --- | --- |\n| one | two |\n" + gap + "C | D | E\n--- | --- | ---\nthree | four | five\n"
    source, rendered, projection = project(tmp_path, body)
    parsed = Rendered(rendered)
    assert parsed.tables == 2
    assert [r["text"] for r in parsed.rows] == ["AB", "onetwo", "CDE", "threefourfive"]
    assert [r["cells"] for r in parsed.rows] == [2, 2, 3, 3]
    assert projection["complete"] is True
    table_rows = [r for r in projection["blocks"] if r["block_id"].startswith("table-row:")]
    assert len(table_rows) == 6
    assert [r["dom_key"] is None for r in table_rows] == [False, True, False, False, True, False]
    assert all(source[r["source_start"]:r["source_end"]].strip() for r in table_rows)


@pytest.mark.parametrize("body", ["A | B\n", "| A | B |\n", "| --- | --- |\n"])
def test_pipe_prose_without_table_header_keeps_literal_text(tmp_path, body):
    _, rendered, projection = project(tmp_path, body)
    assert projection["complete"] is True
    assert "<table" not in rendered
    assert body.strip() in rendered
    assert any(r["dom_key"] is not None for r in projection["blocks"])


def test_leading_only_pipe_row_keeps_existing_canonical_grammar(tmp_path):
    # IR v1 does not classify a leading-only pipe line as a table row. Do not
    # expand block extraction (and therefore persisted anchor identities) here.
    source, rendered, projection = project(tmp_path, "| A | B\n| --- | --- |\none | two |\n")
    assert "| A | B" in rendered
    first = next(r for r in projection["blocks"] if source[r["source_start"]:r["source_end"]].startswith("| A | B"))
    assert first["block_id"].startswith("paragraph:")
    assert first["dom_key"] is not None


@pytest.mark.browser
def test_table_projection_matches_actual_browser_dom(tmp_path):
    """Renderer ownership is checked against Chrome, not just HTMLParser."""
    from browser_support import sync_playwright, launch_chromium
    body = ("A | B\n--- | ---\n`x\\|y😀` | &amp; | discarded\n\n"
            "| C | D | E |\n| --- | --- | --- |\n| one |\n")
    source, rendered, projection = project(tmp_path, body, "\r\n")
    assert projection["complete"] is True
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            page = browser.new_page()
            page.set_content(rendered)
            assert page.locator("table").count() == 2
            actual = page.locator("tr").evaluate_all("nodes => nodes.map(n => ({key: Number(n.dataset.okfProjectionKey), text: n.textContent, cells: n.children.length}))")
            assert [r["text"] for r in actual] == ["AB", "x|y😀&", "CDE", "one"]
            assert [r["cells"] for r in actual] == [2, 2, 3, 3]
            for row in actual:
                record = next(r for r in projection["blocks"] if r["dom_key"] == row["key"])
                for seg in record["segments"]:
                    raw = source[seg["source_start"]:seg["source_end"]]
                    assert ("|" if raw == "\\|" else html.unescape(raw)) == row["text"][seg["dom_start"]:seg["dom_end"]]
        finally:
            browser.close()


@pytest.mark.parametrize("mode,href", [("serve", "/other#target"), ("spa", "/other#target"), ("static", "other.html#target")])
def test_concept_fragment_navigation_stays_internal(tmp_path, mode, href):
    (tmp_path / "note.md").write_text("---\ntype: Note\n---\n# Here\n"
        "[sibling](other.md#target) [self](#here) [absolute](/other.md#target) "
        "[external](https://example.test/other.md#target)\n")
    (tmp_path / "other.md").write_text("---\ntype: Note\n---\n# Target\n")
    bundle = Bundle.load(tmp_path)
    rendered, _ = render_concept_body_with_projection(bundle.concepts[("note",)], bundle, mode=mode)
    links = Rendered(rendered).links
    assert [link["href"] for link in links] == [href, "#here", href, "https://example.test/other.md#target"]
    for link in links[:3]:
        assert link["class"] == "okf-internal"
        assert "target" not in link and "rel" not in link
    assert links[3]["class"] == "okf-external"
    assert links[3]["target"] == "_blank"
    assert set(links[3]["rel"].split()) == {"noopener", "noreferrer"}
    # Scheme-relative URLs remain refused by the URL allowlist, not promoted
    # into bundle-root paths by fragment classification.
    assert Rendered(markdown_to_html("[external](//example.test/x.md#y)")).links == []


def test_lifecycle_events_attribute_mutation_not_original_author(tmp_path, monkeypatch):
    import okf_loom.studio as runtime
    now = "2026-09-01T01:00:00.000Z"
    monkeypatch.setattr(runtime, "_now_iso", lambda: now)
    studio = Studio.for_bundle(tmp_path)
    created = studio.post_comment(concept="note", body="Please fix", actor="user", detail={"future": [1]})
    queue = studio.bus.subscribe()
    now = "2026-09-18T02:00:00.000Z"
    claimed = studio.update_comment(created["id"], state="claimed", claimed_by="aic.Polaris")
    event = queue.get(timeout=1)
    assert (event["operation"], event["actor"], event["ts"]) == ("lifecycle", "aic.Polaris", now)
    assert event["row_token"] == claimed["row_token"]
    assert claimed["claimed_at"] == now
    now = "2026-09-18T03:00:00.000Z"
    resolved = studio.resolve_comment(created["id"], actor="aic.Other", summary="Done")
    event = queue.get(timeout=1)
    assert (event["actor"], event["ts"], event["row_token"]) == ("aic.Other", now, resolved["row_token"])
    now = "2026-09-18T04:00:00.000Z"
    archived, error, _ = studio.update_comment_external(
        created["id"], expected_row_token=resolved["row_token"], archived=True)
    assert error is None
    event = queue.get(timeout=1)
    assert (event["actor"], event["ts"]) == ("user", now)
    assert archived["claimed_by"] == "aic.Other"
    for row in (claimed, resolved, archived):
        assert (row["actor"], row["ts"], row["detail"]) == ("user", created["ts"], {"future": [1]})
    studio.bus.unsubscribe(queue)
    restarted = Studio.for_bundle(tmp_path)
    events = [e for e in restarted.read_events(limit=20) if e["type"] == "comment"]
    assert [e["actor"] for e in events] == ["user", "aic.Polaris", "aic.Other", "user"]
    assert [e["ts"] for e in events] == [created["ts"], claimed["updated_at"], resolved["updated_at"], archived["updated_at"]]
    assert restarted.get_comment(created["id"])["row_token"] == archived["row_token"]


def test_legacy_lifecycle_does_not_invent_actor_or_creation_time(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    legacy = {"id": "01JABED9K0", "concept": "note", "body": "legacy", "state": "resolved",
              "claimed_by": "old-agent", "future": {"keep": True}}
    studio.directives_path.write_text(json.dumps(legacy) + "\n")
    updated = studio.update_comment(legacy["id"], archived=True)
    assert "actor" not in updated and "ts" not in updated
    assert updated["future"] == legacy["future"]
    event = studio.read_events(limit=1)[0]
    assert event["operation"] == "lifecycle" and event["actor"] is None
    assert event["ts"] == updated["updated_at"]
    assert event["claimed_by"] == "old-agent"  # retained, not treated as this actor


def test_cli_claim_and_resolve_publish_their_explicit_actor(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    created = studio.post_comment(concept="note", body="CLI work", actor="user")
    command = Path(__file__).resolve().parents[1] / "scripts/okf-loom"
    for verb, actor, extra in [("comment-claim", "aic.Polaris", ["--no-presence"]),
                               ("comment-resolve", "aic.Resolver", ["--summary", "Done"])]:
        result = subprocess.run([str(command), verb, str(tmp_path), created["id"],
                                 "--actor", actor, "--format", "json", *extra],
                                capture_output=True, text=True, check=True)
        row = json.loads(result.stdout)
        event = [e for e in studio.read_events(limit=20) if e.get("operation") == "lifecycle"][-1]
        assert (event["actor"], event["ts"], event["row_token"]) == (actor, row["updated_at"], row["row_token"])
        assert (row["actor"], row["ts"]) == ("user", created["ts"])


@pytest.mark.parametrize("action,actor", [("reply", "user"), ("claim", "aic.Polaris")])
def test_implicit_parent_unarchive_uses_acting_label_not_parent_author(tmp_path, action, actor):
    studio = Studio.for_bundle(tmp_path)
    parent = studio.post_comment(concept="note", body="thread", actor="user")
    child = studio.post_comment(concept="note", body="clarification", actor="agent",
                                parent_id=parent["id"], state="resolved")
    studio.resolve_comment(parent["id"], actor="old-agent")
    studio.update_comment(parent["id"], archived=True)
    if action == "reply":
        studio.post_comment(concept="note", body="answer", actor=actor, parent_id=parent["id"])
    else:
        studio.update_comment(child["id"], state="claimed", claimed_by=actor)
    event = [e for e in studio.read_events(limit=20) if e["id"] == parent["id"]][-1]
    row = studio.get_comment(parent["id"])
    assert row["archived"] is False
    assert (event["operation"], event["actor"], event["ts"]) == ("lifecycle", actor, row["updated_at"])
    assert (row["actor"], row["ts"], row["claimed_by"]) == ("user", parent["ts"], "old-agent")


LIST_CONTINUATION = "- one😀\n  two\n- parent\n  - child\n  - last\n"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_continuation_newline_transform_has_exact_boundaries(tmp_path, newline):
    from okf_loom.annotations import extract_blocks
    source, rendered, projection = project(tmp_path, LIST_CONTINUATION, newline)
    assert projection["complete"] is True
    assert "one😀\ntwo" in rendered
    blocks = {b.id: b for b in extract_blocks(source)}
    rows = [r for r in projection["blocks"] if r["block_id"].startswith("list-item:")]
    assert len(rows) == 2
    for row, visible in zip(rows, ["one😀\ntwo", "parent\nchild\nlast"]):
        canonical = blocks[row["block_id"]]
        assert (row["source_start"], row["source_end"]) == (canonical.start, canonical.end)
        assert row["dom_key"] is not None
        for seg in row["segments"]:
            raw = source[seg["source_start"]:seg["source_end"]]
            if "\r\n" in raw:
                assert raw == "\r\n", "CRLF must not be mixed with identity text"
                assert seg["dom_end"] - seg["dom_start"] == 1
            assert raw.replace("\r\n", "\n") == visible[seg["dom_start"]:seg["dom_end"]]
    start = source.index("one😀")
    end = source.index("two") + 3
    anchor = capture_anchor(source, start, end)
    assert anchor["exact"] == "one😀" + newline + "  two"
    assert resolve_anchor(anchor, source)["position"] == {"start": start, "end": end}


@pytest.mark.browser
def test_list_continuation_projection_matches_live_dom_and_existing_decoder(tmp_path):
    """Real HTTP/DOM + the shipped JS verifier; no decoder broadening needed."""
    from browser_support import sync_playwright, launch_chromium, live_server
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.md").write_text("# Home\n")
    for name, newline in [("lf", "\n"), ("crlf", "\r\n")]:
        source = ("---\ntype: Note\n---\n\n" + LIST_CONTINUATION).replace("\n", newline)
        (root / (name + ".md")).write_bytes(source.encode())
    with live_server(root) as base, sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            page = browser.new_page()
            page.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
            for name in ("lf", "crlf"):
                page.goto(base + "/" + name)
                page.wait_for_function("name => window.okfLoomStudio?.state.doc?.id === name", arg=name)
                verdict = page.evaluate("""() => {
                    const api = window.okfLoomStudio, doc = api.state.doc;
                    return api._projectionError(doc.annotation_projection,
                        doc.concept_content_rev, doc.annotation_source, doc.html);
                }""")
                assert verdict is None, (name, verdict)
                assert page.locator(".okf-page__body > ul > li").all_text_contents() == [
                    "one😀\ntwo", "parent\nchild\nlast"]
        finally:
            browser.close()
