"""Real browser -> production HTTP -> durable directive -> agent CLI loop.

Every test drives the shipped Studio against a real ``serve`` process on a
disposable loopback bundle.  No comment response is ever fulfilled or mocked:
the only interception is the lost-response case, where the request is delivered
to the real server (which persists it) and the *response* is then dropped, which
is exactly the uncertainty the idempotent retry exists for.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from browser_support import expect, launch_chromium, live_server, sync_playwright
from conftest import okf_module_argv, okf_subprocess_env

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "samples" / "demo_bundle"
TARGET = "notes/review_target"
PAGE_ANCHOR = {"kind": "concept"}
# R3 composer copy: what a comment is, and how precise the draft is.
COMPOSER_PURPOSE = ("A comment asks for work or discussion. It is saved on this bundle for readers and its "
                    "agent, which picks it up through its comment loop; Studio does not start an agent.")
EXACT_PRECISION = ("Exact annotation: pinned to this passage and, once sent, highlighted on the page. It is re-checked "
                   "when the page changes.")
PAGE_PRECISION = "Page comment: about the whole page. No text is highlighted."

TARGET_SOURCE = """---
type: Concept
title: Review target
description: Disposable collaboration fixture.
---

# Review target

Alpha paragraph mentions the shared phrase once.

Second paragraph has caf\u00e9 \U0001F600 marker and the shared phrase again.

- First list item
- Second list item
"""


@pytest.fixture
def studio(tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(DEMO, bundle, ignore=shutil.ignore_patterns(".okf-loom"))
    (bundle / "notes").mkdir(exist_ok=True)
    (bundle / "notes" / "review_target.md").write_text(TARGET_SOURCE, encoding="utf-8")
    with live_server(bundle) as base:
        yield base, bundle


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = context.new_page()
        yield pg
        context.close()
        browser.close()


def _cli(*args, ok=(0,)):
    done = subprocess.run(okf_module_argv(*args), cwd=ROOT, env=okf_subprocess_env(),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode in ok, (args, done.returncode, done.stdout, done.stderr)
    return done


def _directives(bundle, concept=None):
    rows = json.loads(_cli("comment-list", str(bundle), "--format", "json").stdout)
    rows = rows["comments"] if isinstance(rows, dict) else rows
    return [row for row in rows if concept is None or row["concept"] == concept]


def _boot(page, base, concept=TARGET):
    page.goto(f"{base}/{concept}", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    # Assert on the value that settled the wait: a second round trip can observe
    # a later live-reconnect reload ("loading") and fail a healthy boot.
    settled = page.wait_for_function(
        "() => { const s = window.okfLoomStudio.state.commentsStatus;"
        " return ['ready', 'error'].includes(s) ? s : false; }").json_value()
    assert settled == "ready"
    page.wait_for_function("() => window.okfLoomStudio.state.selectionAffordanceReady === true")


def _record_comment_posts(page):
    posts = []

    def on_response(response):
        request = response.request
        if request.method == "POST" and request.url.endswith("/__comment"):
            posts.append({"status": response.status, "body": json.loads(request.post_data),
                          "key": request.headers.get("idempotency-key")})

    page.on("response", on_response)
    return posts


def _wait_for(predicate, timeout_s=5.0, message="condition", sleep=None):
    """Poll a plain Python predicate. ``sleep`` must yield to Playwright's
    event dispatch (pass ``lambda: page.wait_for_timeout(100)``); plain
    ``time.sleep`` blocks the connection and starves event callbacks."""
    if sleep is None:
        sleep = lambda: time.sleep(0.1)  # noqa: E731
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        sleep()
    raise AssertionError(f"timed out waiting for {message}: {last!r}")


def _open_comments(page):
    page.get_by_role("button", name="Comments", exact=True).click()
    expect(page.get_by_role("textbox", name="Comment for agent")).to_be_visible()


def _send(page, body):
    page.get_by_role("textbox", name="Comment for agent").fill(body)
    page.get_by_role("button", name="Send", exact=True).click()


def _expect_posted(page):
    expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")


def _select_occurrence(page, selector, exact, occurrence=0, until=None):
    """Select the nth occurrence of ``exact`` (optionally through ``until``)."""
    page.locator(selector).evaluate(
        """(root, [exact, occurrence, until]) => {
          const nodes = []; let text = '';
          const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          for (let node; (node = walker.nextNode());) { nodes.push({node, start: text.length}); text += node.nodeValue || ''; }
          let start = -1;
          for (let i = 0; i <= occurrence; i++) { start = text.indexOf(exact, start + 1); if (start < 0) throw new Error('occurrence absent'); }
          let end = start + exact.length;
          if (until) { const stop = text.indexOf(until, end); if (stop < 0) throw new Error('until absent'); end = stop + until.length; }
          // A start boundary belongs to the node that BEGINS there, an end
          // boundary to the node that ends there, so a block-initial selection
          // never leaks into the preceding block.
          const point = (offset, isStart) => {
            for (const item of nodes) {
              const itemEnd = item.start + (item.node.nodeValue || '').length;
              if (isStart ? offset < itemEnd : offset <= itemEnd) return [item.node, offset - item.start];
            }
            throw new Error('endpoint absent');
          };
          const range = document.createRange();
          range.setStart(...point(start, true)); range.setEnd(...point(end, false));
          const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
        }""",
        [exact, occurrence, until],
    )


def _comment_on_selection(page):
    action = page.get_by_role("button", name="Comment on the selected text")
    expect(action).to_be_visible()
    action.click()
    expect(page.get_by_role("textbox", name="Comment for agent")).to_be_visible()


def _show_source(page):
    page.get_by_role("group", name="View mode").get_by_role("button", name="Source", exact=True).click()
    expect(page.locator(".okf-source")).to_contain_text("Alpha paragraph")


def test_page_comment_browser_reply_and_agent_loop(studio, page):
    base, bundle = studio
    _boot(page, base)
    posts = _record_comment_posts(page)
    _open_comments(page)
    expect(page.locator(".okf-composer__anchor code")).to_have_text(TARGET)
    # R3: the composer says what a comment is and how precise this one is.
    expect(page.locator("#okf-panel").get_by_role("heading", name="New comment", exact=True)).to_be_visible()
    expect(page.locator(".okf-composer__anchor-note")).to_have_text(PAGE_PRECISION)
    expect(page.locator("#okf-composer-purpose")).to_have_text(COMPOSER_PURPOSE)
    expect(page.get_by_role("textbox", name="Comment for agent")).to_have_accessible_description(COMPOSER_PURPOSE)
    _send(page, "Please record an owner for this table.")
    _expect_posted(page)

    assert [(p["status"], p["body"]["anchor"], p["body"]["concept"]) for p in posts] == [(201, PAGE_ANCHOR, TARGET)]
    [stored] = _directives(bundle)
    assert stored["anchor"] == PAGE_ANCHOR and stored["state"] == "open" and stored["actor"] == "user"
    card = page.locator(f"#comment-{stored['id']}")
    expect(card.locator(".okf-anchor-row__status")).to_contain_text("Page comment")
    expect(card.locator(".okf-anchor-row__cause")).to_have_text("About the whole page; no text is highlighted.")
    expect(card).not_to_contain_text("Anchor unavailable")

    # Supported agent loop: wait -> claim -> clarifying reply.
    work = json.loads(_cli("wait", str(bundle), "--for", "comment", "--timeout", "10").stdout)
    assert work["id"] == stored["id"] and work["anchor"] == PAGE_ANCHOR
    _cli("comment-claim", str(bundle), stored["id"], "--actor", "aic/test", "--no-presence", "--format", "json")
    _cli("comment-reply", str(bundle), stored["id"], "--body", "Which team owns it?")
    agent_reply = next(row for row in _directives(bundle) if row.get("parent_id") == stored["id"])
    assert agent_reply["actor"] == "agent" and agent_reply["state"] == "resolved"
    # The agent's clarification is a resolved statement, never its own new work.
    assert _cli("wait", str(bundle), "--for", "comment", "--timeout", "2", ok=(1,)).stdout.strip() == ""

    # Reload: canonical rows (page anchor + the anchorless agent reply) load.
    _boot(page, base)
    _open_comments(page)
    card = page.locator(f"#comment-{stored['id']}")
    expect(card).to_contain_text("Which team owns it?")
    card.get_by_role("button", name="Reply", exact=True).first.click()
    reply_box = page.get_by_role("textbox", name="Reply to comment")
    reply_box.fill("The data platform team.")
    reply_box.press("Enter")
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    assert (posts[-1]["status"], posts[-1]["body"]["anchor"], posts[-1]["body"]["parent_id"]) == (201, PAGE_ANCHOR, stored["id"])

    # The user's answer IS new work; the agent mutates and resolves.
    answer = json.loads(_cli("wait", str(bundle), "--for", "comment", "--timeout", "10").stdout)
    assert answer["parent_id"] == stored["id"] and answer["body"] == "The data platform team."
    _cli("replace-text", "--bundle", str(bundle), "--id", TARGET, "--old", "First list item",
         "--new", "Owner: data platform team")
    _cli("comment-resolve", str(bundle), answer["id"], "--summary", "noted")
    _cli("comment-resolve", str(bundle), stored["id"], "--summary", "owner recorded")
    assert "Owner: data platform team" in (bundle / "notes" / "review_target.md").read_text(encoding="utf-8")

    _boot(page, base)
    _open_comments(page)
    expect(page.locator(".okf-page__body")).to_contain_text("Owner: data platform team")
    states = {row["id"]: row["state"] for row in _directives(bundle)}
    assert len(states) == 3 and states[stored["id"]] == "resolved" and states[answer["id"]] == "resolved"
    assert _cli("wait", str(bundle), "--for", "comment", "--timeout", "2", ok=(1,)).stdout.strip() == ""


def test_source_selection_posts_exact_canonical_unicode_span(studio, page):
    base, bundle = studio
    _boot(page, base)
    posts = _record_comment_posts(page)
    _show_source(page)
    # The SECOND occurrence, after astral + combining-range text: a fuzzy or
    # first-match mapping would land on the first paragraph.
    _select_occurrence(page, ".okf-source", "the shared phrase", occurrence=1)
    _comment_on_selection(page)
    expect(page.locator(".okf-composer__anchor code")).to_have_text("\u201cthe shared phrase\u201d")
    expect(page.locator(".okf-composer__anchor-note")).to_have_text(EXACT_PRECISION)
    _send(page, "Name the phrase here.")
    _expect_posted(page)

    [post] = posts
    anchor = post["body"]["anchor"]
    assert post["status"] == 201 and anchor["schema"] == 2 and anchor["kind"] == "text-quote"
    source = (bundle / "notes" / "review_target.md").read_text(encoding="utf-8")
    start, end = anchor["position"]["start"], anchor["position"]["end"]
    assert source[start:end] == anchor["exact"] == "the shared phrase"
    assert start == source.rindex("the shared phrase") and "\U0001F600" in anchor["prefix"]
    [stored] = _directives(bundle)
    assert stored["anchor"] == anchor

    # Reload in Rendered view: the persisted span projects onto the right text.
    _boot(page, base)
    mark = page.locator(f".okf-comment-mark[data-comment-id='{stored['id']}']")
    expect(mark).to_have_text("the shared phrase")
    assert "Second paragraph" in mark.locator("xpath=ancestor::p").inner_text()
    _open_comments(page)
    expect(page.locator(f"#comment-{stored['id']} .okf-anchor-row__status")).to_contain_text("Attached")
    expect(page.locator(f"#comment-{stored['id']} .okf-anchor-row__cause")).to_have_text(
        "Exact annotation: pinned to this passage and highlighted.")


@pytest.mark.parametrize("stale_pane, select, reason", [
    (False, {"exact": "shared phrase once.", "until": "Second paragraph"}, "not inside a single source block"),
    (True, {"exact": "First list item"}, "not a byte-exact view of the saved file"),
])
def test_source_selection_without_exact_mapping_is_an_explicit_page_comment(studio, page, stale_pane, select, reason):
    base, bundle = studio
    concept = TARGET
    _boot(page, base, concept)
    posts = _record_comment_posts(page)
    _show_source(page)
    if stale_pane:
        # The pane no longer equals the saved body, so its offsets prove nothing.
        page.locator(".okf-source").evaluate("pane => { pane.textContent = 'Unsaved preamble\\n' + pane.textContent; }")
    _select_occurrence(page, ".okf-source", select["exact"], until=select.get("until"))
    selected = page.evaluate("getSelection().toString().trim()")
    _comment_on_selection(page)
    note = page.locator(".okf-composer__anchor-note")
    expect(note).to_contain_text("Page comment. Your Source selection")
    expect(note).to_contain_text("is quoted in the comment, not attached to exact text")
    expect(note).to_contain_text(reason)
    expect(page.locator(".okf-composer__anchor code")).to_have_text(concept)
    _send(page, "Tighten this passage.")
    _expect_posted(page)

    [post] = posts
    assert post["status"] == 201 and post["body"]["anchor"] == PAGE_ANCHOR
    context = post["body"]["detail"]["selection"]
    assert (context["surface"], context["precision"], context["quote"]) == ("source", "page", selected)
    [stored] = _directives(bundle, concept)
    assert stored["anchor"] == PAGE_ANCHOR and stored["detail"]["selection"]["quote"] == selected

    _boot(page, base, concept)
    assert page.locator(".okf-comment-mark").count() == 0, "a page fallback must never draw a precise highlight"
    _open_comments(page)
    row = page.locator(f"#comment-{stored['id']} .okf-anchor-row")
    expect(row.locator(".okf-anchor-row__status")).to_contain_text("Page comment")
    expect(row).to_contain_text("quoted here, not attached to exact text")
    # The quoted selection explains the row; the whole-page line would contradict it.
    expect(row).not_to_contain_text("no text is highlighted")
    expect(row.locator("q")).to_contain_text(selected.split("\n")[0].strip())


def test_rendered_selection_still_posts_the_exact_anchor(studio, page):
    base, bundle = studio
    _boot(page, base)
    posts = _record_comment_posts(page)
    # R3: the Context Pack action says it is cited evidence and messages no one.
    expect(page.get_by_role("link", name="Copy concept context", exact=True)).to_have_accessible_description(
        "Cited passages for reuse, not a summary. Copying sends nothing to the agent.")
    _select_occurrence(page, ".okf-page__body", "Second list item")
    # R3: the selection feeds two actions; the Comment button says how it
    # differs from Copy selection with citation, and the Studio hint names both.
    expect(page.locator(".okf-selection-hint")).to_have_text(
        "Select text to comment on it, or to copy it with a citation.")
    expect(page.get_by_role("button", name="Comment on the selected text")).to_have_accessible_description(
        "Starts a comment on the selected text. To reuse the text elsewhere, use Copy selection with citation.")
    _comment_on_selection(page)
    expect(page.locator(".okf-composer__anchor-note")).to_have_text(EXACT_PRECISION)
    _send(page, "Rename this item.")
    _expect_posted(page)
    [post] = posts
    anchor = post["body"]["anchor"]
    source = (bundle / "notes" / "review_target.md").read_text(encoding="utf-8")
    assert post["status"] == 201 and post["body"]["detail"] == {}
    assert source[anchor["position"]["start"]:anchor["position"]["end"]] == "Second list item"
    _boot(page, base)
    expect(page.locator(".okf-comment-mark")).to_have_text("Second list item")


def test_graph_selection_falls_back_to_a_page_comment_with_composer(studio, page):
    base, bundle = studio
    page.goto(f"{base}/__graph?selected={TARGET}", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    page.wait_for_function(f"() => (document.getElementById('detail-body') || {{}}).getAttribute?.('data-concept-id') === '{TARGET}'")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'")
    posts = _record_comment_posts(page)
    _select_occurrence(page, "#detail-body", "Alpha paragraph")
    _comment_on_selection(page)
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.locator(".okf-composer__anchor code")).to_have_text(TARGET)
    expect(page.locator(".okf-composer__anchor-note")).to_contain_text("graph preview selection")
    _send(page, "Clarify alpha.")
    _expect_posted(page)

    [post] = posts
    assert (post["status"], post["body"]["concept"], post["body"]["anchor"]) == (201, TARGET, PAGE_ANCHOR)
    assert post["body"]["detail"]["selection"]["surface"] == "graph"
    assert post["body"]["detail"]["selection"]["quote"] == "Alpha paragraph"
    [stored] = _directives(bundle)
    assert stored["concept"] == TARGET and stored["anchor"] == PAGE_ANCHOR

    _boot(page, base)
    assert page.locator(".okf-comment-mark").count() == 0
    _open_comments(page)
    expect(page.locator(f"#comment-{stored['id']} .okf-anchor-row")).to_contain_text("graph preview")


def test_lost_response_retry_persists_exactly_one_directive(studio, page):
    base, bundle = studio
    _boot(page, base)
    delivered = []

    def lose_first_response(route):
        request = route.request
        if request.method != "POST":
            return route.continue_()
        body = json.loads(request.post_data)["body"]
        first = body not in [seen["body"] for seen in delivered]
        delivered.append({"body": body, "key": request.headers.get("idempotency-key")})
        if not first:
            return route.continue_()
        route.fetch()                       # the REAL server handles and persists it
        route.abort("connectionreset")      # ...and the browser never sees the answer

    page.route("**/__comment", lose_first_response)
    posts = _record_comment_posts(page)
    _open_comments(page)
    _send(page, "Persist me exactly once.")
    status = page.locator("#okf-annotation-status")
    expect(status).to_contain_text("Cannot confirm the comment was posted")
    retry = page.get_by_role("button", name="Retry send", exact=True)
    expect(retry).to_be_enabled()
    expect(page.get_by_role("textbox", name="Comment for agent")).to_have_value("Persist me exactly once.")
    assert len(_directives(bundle)) == 1, "the lost response really was persisted server-side"

    retry.click()
    _expect_posted(page)
    expect(page.get_by_role("textbox", name="Comment for agent")).to_have_value("")
    assert [item["key"] for item in delivered] == [delivered[0]["key"]] * 2 and delivered[0]["key"]
    assert [p["status"] for p in posts] == [201]
    [stored] = _directives(bundle)

    # The same guarantee for an inline browser reply.
    card = page.locator(f"#comment-{stored['id']}")
    card.get_by_role("button", name="Reply", exact=True).first.click()
    reply_box = page.get_by_role("textbox", name="Reply to comment")
    reply_box.fill("One reply only.")
    reply_box.press("Enter")
    retry_reply = page.get_by_role("button", name="Retry reply", exact=True)
    expect(retry_reply).to_be_enabled()
    expect(reply_box).to_have_value("One reply only.")
    retry_reply.click()
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    assert delivered[2]["key"] == delivered[3]["key"] != delivered[0]["key"]

    _boot(page, base)
    rows = _directives(bundle)
    assert sorted(row["body"] for row in rows) == ["One reply only.", "Persist me exactly once."]
    assert next(row for row in rows if row["body"] == "One reply only.")["parent_id"] == stored["id"]
    work = json.loads(_cli("wait", str(bundle), "--for", "comment", "--timeout", "10").stdout)
    assert work["id"] == stored["id"] and work["queue"]["pending"] == 1


def test_server_rejection_keeps_the_draft_and_persists_nothing(studio, page):
    base, bundle = studio
    _boot(page, base)
    posts = _record_comment_posts(page)
    _open_comments(page)
    # A real 4xx from the production handler: the body exceeds the 16,384 limit.
    oversized = "x" * 16_385
    _send(page, oversized)
    expect(page.locator("#okf-annotation-status")).to_contain_text("The comment was not posted")
    expect(page.get_by_role("textbox", name="Comment for agent")).to_have_value(oversized)
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
    assert [p["status"] for p in posts] == [400] and _directives(bundle) == []


def test_canonical_row_reading_is_tolerant_of_stored_shapes_but_fails_closed(studio, page):
    base, _bundle = studio
    _boot(page, base)
    verdicts = page.evaluate(
        """(concept) => {
          const studio = window.okfLoomStudio, rev = studio.state.doc.concept_content_rev;
          const resolution = { status: 'invalid', concept_content_rev: rev };
          const check = (anchor, extra) => studio._validateStagedTuple({
            conceptKey: concept, fallbackProjectionRev: rev, eventData: { events: [] },
            commentsData: { comments: [Object.assign({ id: '01JROW', concept, row_token: 'r2:x', anchor,
              anchor_resolution: resolution, future_field: { kept: true } }, extra || {})] },
          });
          const out = {};
          const cases = {
            canonical: { kind: 'concept' }, anchorless: {}, legacyRef: { kind: 'concept', ref: concept },
            legacySource: { kind: 'source', ref: 'old selection', concept },
            foreignRef: { kind: 'concept', ref: 'tables/other' }, foreignConcept: { kind: 'concept', concept: 'tables/other' },
            unknownKind: { kind: 'graph' }, kindless: { ref: 'x' }, array: [],
          };
          for (const [name, anchor] of Object.entries(cases)) {
            const result = check(anchor);
            out[name] = result.skewSignal || (result.comments[0].future_field.kept === true ? 'ok' : 'dropped');
          }
          out.textQuoteNeedsEvidence = check({ kind: 'text-quote', exact: 'x' }, { anchor_resolution: undefined }).skewSignal;
          return out;
        }""",
        TARGET,
    )
    assert verdicts == {
        "canonical": "ok", "anchorless": "ok", "legacyRef": "ok", "legacySource": "ok",
        "foreignRef": "malformed-comment-anchor", "foreignConcept": "malformed-comment-anchor",
        "unknownKind": "malformed-comment-anchor", "kindless": "malformed-comment-anchor",
        "array": "malformed-comment-anchor", "textQuoteNeedsEvidence": "missing-comment-anchor-resolution",
    }


def test_inline_drafts_survive_panel_transitions_and_cancel_cleans_up(studio, page):
    """F3: inline reply/edit composers are owned outside the DOM.

    The comment list is rebuilt on every panel replacement, which used to
    silently destroy any open inline composer (typed text gone, uncertain
    intent gone). The draft owner must restore it against the rebuilt rows,
    and an explicit cancel of a never-dispatched draft must clean it up.
    """
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _send(page, "Root for draft survival.")
    _expect_posted(page)
    [root] = _directives(bundle)
    card = page.locator(f"#comment-{root['id']}")
    expect(card).to_be_visible()

    # Leave a main-composer draft, then an inline reply draft.
    page.get_by_role("textbox", name="Comment for agent").fill("Main composer draft.")
    card.get_by_role("button", name="Reply", exact=True).first.click()
    reply_box = page.get_by_role("textbox", name="Reply to comment")
    reply_box.fill("Inline reply draft.")
    expect(page.locator(".okf-composer__reply-context")).to_contain_text("Root for draft survival.")

    # Close + reopen the panel: the owner must restore the composer.
    page.get_by_role("button", name="Close panel").click()
    expect(page.locator("#okf-panel")).to_be_hidden()
    _open_comments(page)
    expect(page.get_by_role("textbox", name="Comment for agent")).to_have_value("Main composer draft.")
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("Inline reply draft.")
    expect(page.locator(f"#comment-{root['id']} .okf-composer__reply-context")).to_contain_text(
        "Root for draft survival.")

    # A child-panel round trip must also restore it: Studio home -> Changes
    # (the rebuild that destroys the composer DOM) -> Back -> Comments.
    page.get_by_role("button", name="Close panel").click()
    page.locator(".okf-studio-launcher").click()
    expect(page.locator("#okf-panel-title")).to_have_text("Studio")
    page.get_by_role("button", name="Changes").click()
    expect(page.locator("#okf-panel-title")).to_have_text("Changes")
    page.get_by_role("button", name="Back to Studio").click()
    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=True).click()
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("Inline reply draft.")

    # Cancel of a typed (never dispatched) draft deletes it.
    page.locator(".okf-inline-reply").get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_count(0)
    card.get_by_role("button", name="Reply", exact=True).first.click()
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("")
    page.locator(".okf-inline-reply").get_by_role("button", name="Cancel", exact=True).click()

    # The same ownership for the inline edit composer.
    card.get_by_role("button", name="Edit", exact=True).click()
    edit_box = page.get_by_role("textbox", name="Edit comment")
    expect(edit_box).to_have_value("Root for draft survival.")
    edit_box.fill("Edited body draft.")
    page.get_by_role("button", name="Close panel").click()
    page.locator(".okf-studio-launcher").click()
    page.get_by_role("button", name="Changes").click()
    page.get_by_role("button", name="Back to Studio").click()
    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=True).click()
    expect(page.get_by_role("textbox", name="Edit comment")).to_have_value("Edited body draft.")
    page.locator(".okf-inline-reply").get_by_role("button", name="Cancel", exact=True).click()
    card.get_by_role("button", name="Edit", exact=True).click()
    expect(page.get_by_role("textbox", name="Edit comment")).to_have_value("Root for draft survival.")


def test_interrupted_reply_retries_with_the_same_key_and_persists_once(studio, page):
    """F3 + Blocker A: a reply whose response was lost mid-flight keeps its
    uncertain intent across panel replacement, and the retry reuses the
    original Idempotency-Key so the server replays instead of duplicating."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _send(page, "Root for interrupted reply.")
    _expect_posted(page)
    [root] = _directives(bundle)

    dropped = {"armed": True}

    def drop_response(route):
        request = route.request
        if (not dropped["armed"] or request.method != "POST"
                or json.loads(request.post_data).get("parent_id") != root["id"]):
            return route.continue_()
        route.fetch()  # the REAL server persists the reply
        route.fulfill(status=503, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "injected outage"}))

    page.route("**/__comment", drop_response)
    posts = _record_comment_posts(page)
    card = page.locator(f"#comment-{root['id']}")
    card.get_by_role("button", name="Reply", exact=True).first.click()
    reply_box = page.get_by_role("textbox", name="Reply to comment")
    reply_box.fill("Uncertain reply draft.")
    reply_box.press("Enter")
    retry = page.get_by_role("button", name="Retry reply", exact=True)
    expect(retry).to_be_enabled()
    expect(reply_box).to_have_value("Uncertain reply draft.")
    # The POST transport policy retries the injected 5xx three times (same
    # key, server persists once) before the composer settles uncertain.
    def _reply_attempts():
        statuses = [p["status"] for p in posts]
        return statuses if statuses == [503, 503, 503] else None

    _wait_for(_reply_attempts, message="the dropped reply attempts",
              sleep=lambda: page.wait_for_timeout(100))
    original_key = posts[0]["key"]
    assert original_key and posts[1]["key"] == original_key and posts[2]["key"] == original_key
    replies = [row for row in _directives(bundle) if row.get("parent_id") == root["id"]]
    assert len(replies) == 1 and replies[0]["body"] == "Uncertain reply draft."

    # Interrupt: leaving the panel destroys the composer DOM. The uncertain
    # intent must survive in the owner and be restored against the new rows.
    page.get_by_role("button", name="Close panel").click()
    page.locator(".okf-studio-launcher").click()
    page.get_by_role("button", name="Changes").click()
    page.get_by_role("button", name="Back to Studio").click()
    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=True).click()
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("Uncertain reply draft.")
    retry = page.get_by_role("button", name="Retry reply", exact=True)
    expect(retry).to_be_enabled()
    expect(page.locator(".okf-inline-reply")).to_contain_text("Cannot confirm the reply was posted")

    dropped["armed"] = False
    retry.click()
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    _wait_for(lambda: [p["status"] for p in posts] == [503, 503, 503, 201],
              message="the retry response", sleep=lambda: page.wait_for_timeout(100))
    assert posts[3]["key"] == original_key, "the retry must reuse the same Idempotency-Key"
    replies = [row for row in _directives(bundle) if row.get("parent_id") == root["id"]]
    assert len(replies) == 1, "a same-key retry must never create a duplicate"
    # Success closes the intent: reopening Reply starts clean.
    card.get_by_role("button", name="Reply", exact=True).first.click()
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("")


def test_held_edit_success_cannot_erase_a_replacement_edit_draft(studio, page):
    """F3: an inline edit whose successful response is HELD while the user
    leaves and reopens Comments, then types a second UNSENT edit into the
    replacement editor. The departed save callback still settles canonically
    (the edit did persist server-side), but its cleanup must not delete the
    replacement editor's newer draft under the shared row key."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _send(page, "Edit race root.")
    _expect_posted(page)
    [row] = _directives(bundle)
    card = page.locator(f"#comment-{row['id']}")

    held = {"route": None, "response": None, "armed": True}

    def hold_first_update(route):
        if held["armed"] and route.request.method == "POST":
            held["armed"] = False
            held["route"] = route
            held["response"] = route.fetch()  # the REAL server applies edit #1
            return
        route.continue_()

    page.route("**/__comment-update", hold_first_update)
    card.get_by_role("button", name="Edit", exact=True).click()
    edit_box = page.get_by_role("textbox", name="Edit comment")
    edit_box.fill("First edit body.")
    page.get_by_role("button", name="Save", exact=True).click()
    _wait_for(lambda: held["route"] is not None, message="the held edit POST",
              sleep=lambda: page.wait_for_timeout(100))

    # The first edit really persisted server-side while its response is held.
    def _first_edit_persisted():
        rows = _directives(bundle)
        return any(r["id"] == row["id"] and r["body"] == "First edit body." for r in rows)

    _wait_for(_first_edit_persisted, message="edit #1 to persist server-side")

    # Leave and reopen Comments: a replacement editor restores the draft.
    page.get_by_role("button", name="Close panel").click()
    _open_comments(page)
    expect(page.get_by_role("textbox", name="Edit comment")).to_have_value("First edit body.")
    # Type the second, UNSENT edit into the replacement editor.
    page.get_by_role("textbox", name="Edit comment").fill("Second unsent edit.")

    # Release the old success. The departed callback settles canonically but
    # must not erase the replacement editor's newer draft.
    held["route"].fulfill(response=held["response"])
    expect(page.get_by_text("Comment updated.")).to_be_visible()
    assert "First edit body." in card.inner_text(), "the canonical row is adopted"
    expect(page.get_by_role("textbox", name="Edit comment")).to_have_value("Second unsent edit.")

    # Another panel round trip retains the second text.
    page.get_by_role("button", name="Close panel").click()
    _open_comments(page)
    expect(page.get_by_role("textbox", name="Edit comment")).to_have_value("Second unsent edit.")
    page.unroute("**/__comment-update")


def test_held_reply_success_preserves_replacement_draft_and_replay_identity(studio, page):
    """F3: an inline reply whose successful response is HELD while the user
    reopens Comments and types a replacement draft. Releasing the late
    success must persist (exactly once) the reply it confirms, must not
    delete the replacement draft, and the replacement's later dispatch must
    carry its own fresh Idempotency-Key — proven against persisted rows and
    recorded POSTs, not in-memory fields."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _send(page, "Reply race root.")
    _expect_posted(page)
    [root] = _directives(bundle)
    card = page.locator(f"#comment-{root['id']}")
    posts = _record_comment_posts(page)

    held = {"route": None, "response": None, "armed": True}

    def hold_first_reply(route):
        request = route.request
        if (held["armed"] and request.method == "POST"
                and json.loads(request.post_data).get("parent_id") == root["id"]):
            held["armed"] = False
            held["route"] = route
            held["response"] = route.fetch()  # the REAL server persists reply #1
            return
        route.continue_()

    page.route("**/__comment", hold_first_reply)
    card.get_by_role("button", name="Reply", exact=True).first.click()
    reply_box = page.get_by_role("textbox", name="Reply to comment")
    reply_box.fill("First reply body.")
    reply_box.press("Enter")
    _wait_for(lambda: held["route"] is not None, message="the held reply POST",
              sleep=lambda: page.wait_for_timeout(100))
    _wait_for(lambda: [r["body"] for r in _directives(bundle)
                       if r.get("parent_id") == root["id"]] == ["First reply body."],
              message="reply #1 to persist exactly once server-side")

    # Round trip: the replacement composer restores the uncertain intent.
    page.get_by_role("button", name="Close panel").click()
    _open_comments(page)
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("First reply body.")
    expect(page.get_by_role("button", name="Retry reply", exact=True)).to_be_enabled()
    # Type the newer, UNSENT replacement text over the restored composer.
    page.get_by_role("textbox", name="Reply to comment").fill("Second unsent reply.")

    # Release the older success: reply #1 is adopted canonically and the
    # replacement draft must survive it.
    held["route"].fulfill(response=held["response"])
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("Second unsent reply.")

    # Another round trip retains the replacement text and its intent.
    page.get_by_role("button", name="Close panel").click()
    _open_comments(page)
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("Second unsent reply.")
    # Q4: changed text is not a retry of the unconfirmed reply: no same-key
    # Retry, and the status says the earlier reply may already be posted.
    reply = page.locator(".okf-inline-reply")
    expect(reply.get_by_role("button", name="Retry reply", exact=True)).to_have_count(0)
    expect(reply.locator(".okf-composer__status")).to_contain_text("may already be posted")

    # Dispatching the replacement (a different body) must use a FRESH key —
    # never the first reply's key — and persist exactly one new row.
    reply.get_by_role("button", name="Reply", exact=True).click()
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    _wait_for(lambda: [p["status"] for p in posts] == [201, 201],
              message="both browser-issued reply POSTs", sleep=lambda: page.wait_for_timeout(100))
    replies = [r for r in _directives(bundle) if r.get("parent_id") == root["id"]]
    assert sorted(r["body"] for r in replies) == ["First reply body.", "Second unsent reply."], (
        "the released old response must not duplicate reply #1 nor lose reply #2")
    assert posts[0]["key"] and posts[1]["key"] and posts[0]["key"] != posts[1]["key"], (
        "the replacement intent must dispatch under its own fresh Idempotency-Key")
    page.unroute("**/__comment")


def test_late_reply_failure_cannot_corrupt_a_newer_intents_key_or_body(studio, page):
    """F3: a first reply left in flight is interrupted, and the user's
    replacement editor dispatches a NEWER intent (changed body → fresh key)
    while the older POST's definite failure finally settles. The late
    outcome must not overwrite the newer intent's key/body/state: the
    replacement persists exactly once under its own key and closes cleanly."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _send(page, "Late failure root.")
    _expect_posted(page)
    [root] = _directives(bundle)
    card = page.locator(f"#comment-{root['id']}")
    posts = _record_comment_posts(page)

    ctl = {"keys": [], "holds": {}}

    def manage_reply(route):
        request = route.request
        if (request.method != "POST"
                or json.loads(request.post_data).get("parent_id") != root["id"]):
            return route.continue_()
        key = request.headers.get("idempotency-key") or ""
        if key not in ctl["keys"]:
            ctl["keys"].append(key)
            if len(ctl["keys"]) == 1:
                ctl["holds"][key] = route       # hold attempt #1; nothing persisted
                return
            resp = route.fetch()                # newer intent: really persisted
            ctl["holds"][key] = (route, resp)   # ...with its response held
            return
        # The older key's later attempts: definite failure, never persisted.
        route.fulfill(status=503, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "injected outage"}))

    page.route("**/__comment", manage_reply)
    card.get_by_role("button", name="Reply", exact=True).first.click()
    reply_box = page.get_by_role("textbox", name="Reply to comment")
    reply_box.fill("First attempt body.")
    reply_box.press("Enter")
    _wait_for(lambda: ctl["keys"], message="the first reply dispatch",
              sleep=lambda: page.wait_for_timeout(100))
    first_key = ctl["keys"][0]

    # Interrupt mid-flight; the replacement session restores the intent.
    page.get_by_role("button", name="Close panel").click()
    _open_comments(page)
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_value("First attempt body.")
    expect(page.get_by_role("button", name="Retry reply", exact=True)).to_be_enabled()

    # A changed body is a NEWER intent: a fresh key, held in flight (Q4: it is
    # sent with Reply, not offered as a Retry of the unconfirmed one).
    page.get_by_role("textbox", name="Reply to comment").fill("Replacement body.")
    page.locator(".okf-inline-reply").get_by_role("button", name="Reply", exact=True).click()
    _wait_for(lambda: len(ctl["keys"]) >= 2, message="the newer intent dispatch",
              sleep=lambda: page.wait_for_timeout(100))
    second_key = ctl["keys"][1]
    assert second_key and second_key != first_key

    # Now the older intent's failure finally lands: three bounded attempts,
    # none persisted. Its late outcome must leave the newer intent alone.
    ctl["holds"][first_key].fulfill(status=503, content_type="application/json",
                                    body=json.dumps({"ok": False, "error": "injected outage"}))
    _wait_for(lambda: [p["status"] for p in posts if p["key"] == first_key] == [503, 503, 503],
              message="the late first-intent failure attempts",
              sleep=lambda: page.wait_for_timeout(100))

    # Release the newer intent's held success: it must settle under its OWN
    # key and close the draft cleanly (a corrupted entry would linger).
    second_route, second_response = ctl["holds"][second_key]
    second_route.fulfill(response=second_response)
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    _wait_for(lambda: [p["status"] for p in posts if p["status"] == 201] == [201],
              message="the replacement's persisted success", sleep=lambda: page.wait_for_timeout(100))

    replies = [r for r in _directives(bundle) if r.get("parent_id") == root["id"]]
    assert [r["body"] for r in replies] == ["Replacement body."], (
        "the failed first attempt must never persist; the replacement persists exactly once")
    assert [p["key"] for p in posts if p["status"] == 201] == [second_key], (
        "the persisted success must be the newer intent's own key")
    card.get_by_role("button", name="Reply", exact=True).first.click()
    assert page.get_by_role("textbox", name="Reply to comment").input_value() == "", (
        "the newer intent must close cleanly despite the late older failure")
    page.unroute("**/__comment")


def _composer_focus_is_retained(page) -> bool:
    """F4: the focused control survives a refresh — the very same connected
    composer textarea, never a dropped focus on <body>."""
    return page.evaluate(
        """() => { const ta = document.querySelector('.okf-comment-composer-section .okf-composer__textarea');
          return !!ta && document.activeElement === ta && ta.isConnected; }""")


def test_refresh_publishes_readiness_before_rendering_and_recovers_from_failure(studio, page):
    """F4: a successful refresh must never leave the panel's aria-busy
    loading node behind (readiness publishes BEFORE render), and a failed
    refresh must show its explicit error + Retry without wiping the draft.
    The focused control (not just its value) is retained through both."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _send(page, "Row that must survive refreshes.")
    _expect_posted(page)
    [row] = _directives(bundle)
    expect(page.locator(f"#comment-{row['id']}")).to_be_visible()

    composer = page.get_by_role("textbox", name="Comment for agent")
    composer.fill("Draft kept across refreshes.")
    expect(composer).to_be_focused()

    def slow_comments(route):
        time.sleep(0.6)
        route.continue_()

    page.route("**/__comments*", slow_comments)
    page.evaluate("void window.okfLoomStudio._loadComments()")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'")
    # The F4 defect published readiness after the pre-render snapshot: the
    # status said ready while the DOM still showed the aria-busy node.
    assert page.locator(".okf-panel-state[aria-busy='true']").count() == 0
    assert page.locator(f"#comment-{row['id']}").count() == 1
    expect(composer).to_have_value("Draft kept across refreshes.")
    assert _composer_focus_is_retained(page)

    page.unroute("**/__comments*")

    def fail_comments(route):
        return route.fulfill(status=500, content_type="application/json",
                             body=json.dumps({"ok": False, "error": "injected outage"}))

    page.route("**/__comments*", fail_comments)
    page.evaluate("void window.okfLoomStudio._loadComments()")
    error_node = page.locator(".okf-panel-state--error")
    expect(error_node).to_be_visible()
    expect(error_node).to_contain_text("Comments could not be loaded")
    expect(composer).to_have_value("Draft kept across refreshes.")
    assert page.locator(f"#comment-{row['id']}").count() == 1, "last confirmed data stays visible"
    assert _composer_focus_is_retained(page)

    page.unroute("**/__comments*")
    page.get_by_role("button", name="Retry loading comments").click()
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'")
    assert page.locator(".okf-panel-state--error").count() == 0
    assert page.locator(".okf-panel-state[aria-busy='true']").count() == 0
    assert page.locator(f"#comment-{row['id']}").count() == 1
    expect(composer).to_have_value("Draft kept across refreshes.")


def test_refresh_empty_success_stays_correctly_empty_and_retains_draft_and_focus(studio, page):
    """F4: a delayed refresh that succeeds with ZERO rows must settle into
    the explicit empty state — no lingering aria-busy loading node, no
    phantom rows — while keeping the composer draft and the focused control
    intact (the first-load-empty panel is the F4 surface)."""
    base, bundle = studio
    _boot(page, base)  # fresh disposable bundle: no comments on the target yet
    _open_comments(page)
    expect(page.locator(".okf-comment-list")).to_contain_text("No comments yet")
    expect(page.locator(".okf-comment-list")).to_contain_text(
        "Comments save requests and discussion about this page for readers and its agent.")
    assert page.locator(".okf-comment").count() == 0

    composer = page.get_by_role("textbox", name="Comment for agent")
    composer.click()
    composer.fill("Draft kept on the empty refresh.")
    expect(composer).to_be_focused()

    def slow_comments(route):
        time.sleep(0.6)
        route.continue_()

    page.route("**/__comments*", slow_comments)
    page.evaluate("void window.okfLoomStudio._loadComments()")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'")
    assert page.locator(".okf-panel-state[aria-busy='true']").count() == 0
    expect(page.locator(".okf-comment-list")).to_contain_text("No comments yet")
    assert page.locator(".okf-comment").count() == 0, "an empty success stays correctly empty"
    expect(composer).to_have_value("Draft kept on the empty refresh.")
    assert _composer_focus_is_retained(page)


def test_owned_supersession_stays_quiet_while_genuine_failure_stays_visible(studio, page):
    """F15: an owned superseded load abort is expected and logs at debug; a
    genuine transport failure still logs an error with a visible recovery."""
    base, bundle = studio
    _boot(page, base)
    console = []
    page.on("console", lambda m: console.append((m.type, m.text)))

    # Owned abort: bump the content revision while a refresh is in flight.
    # The local server answers fast, so hold the GET briefly to guarantee the
    # revision commit lands mid-load.
    def slow_comments(route):
        time.sleep(0.4)
        route.continue_()

    page.route("**/__comments*", slow_comments)
    page.evaluate("""() => {
      const s = window.okfLoomStudio;
      void s._loadComments();
      setTimeout(() => {
        const clone = JSON.parse(JSON.stringify(s.state.doc));
        clone.concept_content_rev = 'f'.repeat(64);
        s.applyDoc(clone);
      }, 120);
    }""")
    _wait_for(lambda: any(kind == "debug" and "superseded by a newer owner" in text
                          for kind, text in console),
              message="the owned-supersession debug line",
              sleep=lambda: page.wait_for_timeout(100))
    owned_errors = [text for kind, text in console
                    if kind == "error" and "load comments/events failed" in text]
    assert owned_errors == [], "an owned supersession must not log an error"

    # Settle back onto the real document, then force a genuine failure.
    page.unroute("**/__comments*")
    page.evaluate("void window.okfLoomStudio._loadComments()")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'")

    def abort_comments(route):
        return route.abort("connectionreset")

    page.route("**/__comments*", abort_comments)
    _open_comments(page)  # panel DOM for the error node; guarded load needs a kick
    page.evaluate("void window.okfLoomStudio._loadComments()")
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'error'", timeout=20000)
    assert any(kind == "error" and "load comments/events failed" in text for kind, text in console), (
        "a genuine failure must stay observable in the console")
    expect(page.get_by_role("button", name="Retry loading comments")).to_be_visible()
    page.unroute("**/__comments*")
    page.get_by_role("button", name="Retry loading comments").click()
    page.wait_for_function("() => window.okfLoomStudio.state.commentsStatus === 'ready'")


def test_lazy_source_stale_response_cannot_regress_a_committed_document(studio, page):
    """F6: a held stale GET /__data/doc released AFTER a live commit must not
    roll the document back (hold-A / commit-B / release-A)."""
    base, bundle = studio
    held = {"done": False, "route": None, "response": None}

    def hold_first_doc(route):
        if held["done"] or route.request.method != "GET":
            return route.continue_()
        held["done"] = True
        held["route"] = route
        held["response"] = route.fetch()  # revision A, delivered late

    page.route("**/__data/doc*", hold_first_doc)
    page.goto(f"{base}/{TARGET}?view=source", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    # The boot-time source read is the HELD one: the pane stays empty (or
    # loading) until the live commit delivers revision B below.
    assert page.evaluate("typeof window.okfLoomStudio === 'object'")

    _cli("replace-text", "--bundle", str(bundle), "--id", TARGET,
         "--old", "Alpha paragraph", "--new", "Alpha paragraph BMARK")
    # The live commit delivers revision B and the source pane follows it.
    expect(page.locator(".okf-source")).to_contain_text("BMARK", timeout=15000)
    gen_b = page.evaluate("window.okfLoomStudio.state.documentGeneration")
    rev_b = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")

    held["route"].fulfill(response=held["response"])  # release the stale revision-A answer
    page.wait_for_timeout(500)
    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == rev_b, (
        "the stale response must not roll the document revision back")
    assert page.evaluate("window.okfLoomStudio.state.documentGeneration") == gen_b
    expect(page.locator(".okf-source")).to_contain_text("BMARK")
    page.unroute("**/__data/doc*")


def test_cold_source_first_publication_fences_stale_lazy_response(studio, page):
    """F6: the FIRST canonical publication (null→doc) does not advance
    documentGeneration, so the generation fence alone admits a lazy Source
    response that predates it. Arrange revision B as the first canonical
    commit while the pane's doc state is still null, then let a lazy Source
    read return with the OLDER revision-A body, and assert every projection —
    Rendered text, Source text, canonical doc rev and the revision-qualified
    annotation projection — remains B with no false loading/error indicator.

    The three cold-boot /__data/doc reads (lazy Source, comments/doc reader,
    live initial reconciliation) are held so nothing commits; they are
    settled with fresh same-revision fetches, and the stale body is then
    delivered to one unambiguous NEW Source read (the only /__data/doc
    reader that runs while state.doc is populated)."""
    base, bundle = studio
    boot_holds = []  # cold-boot /__data/doc reads held while armed
    armed = {"on": True}

    def hold_while_armed(route):
        if not armed["on"] or route.request.method != "GET":
            return route.continue_()
        boot_holds.append(route)

    page.route("**/__data/doc*", hold_while_armed)
    page.goto(f"{base}/{TARGET}?view=source", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    # The three cold-boot canonical reads are held, so NOTHING can commit.
    _wait_for(lambda: len(boot_holds) >= 3, message="the three held boot doc reads",
              sleep=lambda: page.wait_for_timeout(100))
    assert page.evaluate("window.okfLoomStudio.state.doc") is None, (
        "no canonical document may commit while every doc read is held")
    # Capture the pre-change (revision A) response body from one held read.
    stale_a = boot_holds[0].fetch()

    # Publish revision B on the server. The live `changed` patch issues the
    # next /__data/doc read (route disarmed) and commits it as the FIRST
    # canonical publication: state.doc is still null, so documentGeneration
    # does not advance — the exact admission gap under repair.
    armed["on"] = False
    _cli("replace-text", "--bundle", str(bundle), "--id", TARGET,
         "--old", "Alpha paragraph", "--new", "Alpha paragraph BMARK")
    expect(page.locator(".okf-source")).to_contain_text("BMARK", timeout=15000)
    rev_b = page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev")
    gen_b = page.evaluate("window.okfLoomStudio.state.documentGeneration")
    assert rev_b and gen_b == 0, "B must be the first canonical publication"

    # Settle every held boot read with a fresh fetch (revision B for all —
    # same-revision re-commits are harmless and must stay admitted).
    for route in boot_holds:
        route.fulfill(response=route.fetch())
    boot_holds.clear()

    # A NEW lazy Source read whose response still carries revision A. With
    # state.doc populated only the Source pane issues this read, so the stale
    # body reaches exactly the fenced admission path.
    armed["on"] = True
    page.evaluate("void window.okfLoomStudio._ensureSourceLoaded(true)")
    _wait_for(lambda: len(boot_holds) >= 1, message="the forced stale source read",
              sleep=lambda: page.wait_for_timeout(100))
    armed["on"] = False
    boot_holds[0].fulfill(response=stale_a)
    page.wait_for_timeout(500)

    assert page.evaluate("window.okfLoomStudio.state.doc.concept_content_rev") == rev_b, (
        "the stale response must not roll the first publication back")
    assert page.evaluate("window.okfLoomStudio.state.documentGeneration") == gen_b
    expect(page.locator(".okf-page__body")).to_contain_text("BMARK")
    expect(page.locator(".okf-source")).to_contain_text("BMARK")
    assert page.evaluate(
        "window.okfLoomStudio.state.doc.annotation_projection.concept_content_rev") == rev_b, (
        "the annotation projection stays revision-qualified at B")
    # No false loading/error indicator remains after the fenced rejection.
    expect(page.locator(".okf-source-state")).to_be_hidden()
    assert page.locator(".okf-source-state--error").count() == 0
    page.unroute("**/__data/doc*")


def test_split_loading_notice_settles_when_another_publication_fills_source(studio, page):
    """H3: in Split, "Loading source…" describes the Source pane, not one
    request. When another canonical publication (here a live patch; on a
    cold boot the comments/document read) fills the pane while the lazy
    Source read is still in flight, the notice must settle at once instead of
    sitting above the loaded panes until the redundant read lands."""
    base, bundle = studio
    held = []
    armed = {"on": True}

    def hold_while_armed(route):
        if not armed["on"] or route.request.method != "GET":
            return route.continue_()
        held.append(route)

    page.route("**/__data/doc*", hold_while_armed)
    page.goto(f"{base}/{TARGET}?view=split", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    _wait_for(lambda: len(held) >= 2, message="the held boot doc reads (incl. the lazy Source read)",
              sleep=lambda: page.wait_for_timeout(100))
    assert page.evaluate("document.querySelector('.okf-view').dataset.okfView") == "split"
    notice = page.locator(".okf-source-state")
    expect(notice).to_be_visible()
    expect(notice).to_have_text("Loading source…")

    # A live patch publishes revision B through the canonical commit owner
    # while every earlier doc read (including the lazy Source read) stays held.
    armed["on"] = False
    _cli("replace-text", "--bundle", str(bundle), "--id", TARGET,
         "--old", "Alpha paragraph", "--new", "Alpha paragraph SPLITMARK")
    expect(page.locator(".okf-source")).to_contain_text("SPLITMARK", timeout=15000)
    assert held, "the lazy Source read must still be in flight for this check"
    expect(notice).to_be_hidden()
    assert page.evaluate("document.querySelector('.okf-view').dataset.okfView") == "split"

    # The held reads then land (same revision): nothing false reappears.
    for route in held:
        route.fulfill(response=route.fetch())
    held.clear()
    page.wait_for_timeout(500)
    expect(notice).to_be_hidden()
    assert page.locator(".okf-source-state--error").count() == 0
    expect(page.locator(".okf-source")).to_contain_text("SPLITMARK")
    expect(page.locator(".okf-page__body")).to_contain_text("SPLITMARK")
    page.unroute("**/__data/doc*")


def test_cold_source_failure_is_explicit_and_retryable(studio, page):
    """F6: a cold ?view=source boot whose document fetch fails shows the
    explicit unavailable state (never a fake-empty source pane) with Retry."""
    base, bundle = studio
    failing = {"armed": True}

    def fail_doc(route):
        if failing["armed"]:
            return route.fulfill(status=500, content_type="application/json",
                                 body=json.dumps({"ok": False, "error": "injected source outage"}))
        return route.continue_()

    page.route("**/__data/doc*", fail_doc)
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(f"{base}/{TARGET}?view=source", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")

    state_node = page.locator(".okf-source-state--error")
    expect(state_node).to_be_visible()
    expect(state_node).to_contain_text("Source could not be loaded")
    expect(state_node).to_contain_text("The rendered document is unchanged")
    retry = page.get_by_role("button", name="Retry loading source")
    expect(retry).to_be_visible()
    assert (page.evaluate("(document.querySelector('.okf-source') || {}).textContent || ''") or "") == ""
    assert page.evaluate("window.okfLoomStudio.state.doc") in (None, {}), "nothing unverified may commit"
    assert any("source load failed" in text for text in errors), "a genuine failure must stay observable"

    failing["armed"] = False
    retry.click()
    expect(page.locator(".okf-source")).to_contain_text("Alpha paragraph")
    expect(page.locator(".okf-source-state--error")).to_have_count(0)
    assert page.evaluate(
        "!!(window.okfLoomStudio.state.doc && window.okfLoomStudio.state.doc.concept_content_rev)")
