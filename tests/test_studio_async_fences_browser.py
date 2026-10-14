"""Q4: asynchronous ordering and post-ownership fences in the live Studio.

Every test drives the shipped Studio against a real ``serve`` process on a
disposable bundle. Requests are held with ``page.route`` and released
explicitly; waits are on page state, never on fixed sleeps.

- A canonical document read that started before the page's first (or any
  newer) publication of a different revision never commits over it: the
  comments reader re-reads, text capture uses the committed document, and a
  live patch re-reads once.
- Inline reply and edit text boxes are read-only while sending; an unconfirmed
  reply whose text is then changed stops promising "no duplicate".
- A main-composer post always settles: a page change or a post-success refresh
  that loses ownership never leaves "Posting comment…" behind, and a cancelled
  attempt's late result only updates the comments list.
- Follow-ups: Retry refresh never posts again; an unconfirmed post keeps its
  key while the draft is the sent one (a changed draft is sent as new, with a
  warning); a 2xx with an unreadable body is unconfirmed; an in-document
  Back/Forward orphans neither the composer's post nor a reply; a same-revision
  re-commit is not a publication; a stalled edit save unlocks.
- Final round: only a revision change confirms an accepted post whose refresh
  failed (never Back/Forward within the page, never the graph); a stalled
  answer body settles at the post's deadline; Esc and Cancel keep an
  unconfirmed post's key rules; a quick action never replaces a draft that is
  being posted.
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

pytestmark = pytest.mark.browser

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "samples" / "demo_bundle"
TARGET = "notes/review_target"
PAGE_ANCHOR = {"kind": "concept"}
NEWER_REV = "f" * 64
UNCERTAIN_COMMENT = "Cannot confirm the comment was posted. Retrying is safe and won’t create a duplicate."
UNCERTAIN_REPLY = "Cannot confirm the reply was posted. Retrying the same text is safe and won’t create a duplicate."
EDITED_REPLY = ("Your earlier reply may already be posted. Sending this changed text posts it as a new reply; "
                "check the thread first.")
CHANGED_COMMENT = ("Your earlier comment may already be posted. Sending this changed comment posts it as a new "
                   "comment; check the comments first.")
ACCEPTED_UNCONFIRMED = ("Comment accepted, but the page could not be refreshed to confirm it. Retry refresh "
                        "re-checks without re-posting.")

TARGET_SOURCE = """---
type: Concept
title: Review target
description: Disposable collaboration fixture.
---

# Review target

Alpha paragraph mentions the shared phrase once.

Second paragraph has the shared phrase again.

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
def graph_studio(tmp_path):
    bundle = tmp_path / "graph"
    (bundle / "sub").mkdir(parents=True)
    (bundle / "index.md").write_text("# Home\n\nRoot overview.\n", encoding="utf-8")
    (bundle / "sub" / "one.md").write_text("---\ntype: Note\ntitle: One\n---\n\n# One\n\nFirst.\n", encoding="utf-8")
    (bundle / "sub" / "two.md").write_text("---\ntype: Note\ntitle: Two\n---\n\n# Two\n\nSecond.\n", encoding="utf-8")
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


def _cli(*args):
    done = subprocess.run(okf_module_argv(*args), cwd=ROOT, env=okf_subprocess_env(),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, (args, done.returncode, done.stdout, done.stderr)
    return done


def _directives(bundle):
    rows = json.loads(_cli("comment-list", str(bundle), "--format", "json").stdout)
    return rows["comments"] if isinstance(rows, dict) else rows


def _wait_for(page, predicate, message, timeout_s=15.0):
    """Poll a Python predicate while yielding to Playwright's event dispatch."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        page.wait_for_timeout(50)
    raise AssertionError(f"timed out waiting for {message}: {last!r}")


def _settled_comments(page):
    return page.wait_for_function(
        "() => { const s = window.okfLoomStudio.state.commentsStatus;"
        " return ['ready', 'error'].includes(s) ? s : false; }").json_value()


def _boot(page, base, concept=TARGET):
    page.goto(f"{base}/{concept}", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    assert _settled_comments(page) == "ready"
    page.wait_for_function("() => window.okfLoomStudio.state.selectionAffordanceReady === true")
    # Live's own first reconciliation has committed too: no boot read is left.
    page.wait_for_function("(id) => !!window.okfLoomLive._getLastDocRevForTest(id)", arg=concept)


def _rev(page):
    return page.evaluate("() => window.okfLoomStudio.state.doc && window.okfLoomStudio.state.doc.concept_content_rev")


def _publish_newer_revision(page, marker="NEWER-COMMIT"):
    """Commit a newer revision through Studio's own commit path, as a live
    patch or a comments load would; the body shows ``marker``."""
    page.evaluate("""(marker) => {
      const s = window.okfLoomStudio, clone = JSON.parse(JSON.stringify(s.state.doc));
      clone.concept_content_rev = 'f'.repeat(64);
      clone.html = clone.html.replace('Alpha paragraph', 'Alpha paragraph ' + marker);
      if (!s.applyDoc(clone)) throw new Error('newer revision did not commit');
    }""", marker)


def _open_comments(page):
    page.get_by_role("button", name="Comments", exact=True).click()
    expect(page.get_by_role("textbox", name="Comment for agent")).to_be_visible()


def _send(page, body):
    page.get_by_role("textbox", name="Comment for agent").fill(body)
    page.get_by_role("button", name="Send", exact=True).click()


def _expect_posted(page):
    expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")


def _post_root(page, bundle, body):
    _open_comments(page)
    _send(page, body)
    _expect_posted(page)
    [root] = _directives(bundle)
    return root


def _replies(bundle, parent_id):
    return [row for row in _directives(bundle) if row.get("parent_id") == parent_id]


def _select_text(page, selector, exact):
    page.locator(selector).evaluate(
        """(root, exact) => {
          const nodes = []; let text = '';
          const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          for (let node; (node = walker.nextNode());) { nodes.push({node, start: text.length}); text += node.nodeValue || ''; }
          const start = text.indexOf(exact), end = start + exact.length;
          if (start < 0) throw new Error('selection text is absent');
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
        }""", exact)


def _composer_settled(page):
    """The composer's post is no longer pending (its own settle signal)."""
    page.wait_for_function(
        "() => { const cp = window.okfLoomStudio.state.composerPost;"
        " return !cp || cp.outcome !== 'pending'; }")


# ---- (a) canonical readers against a newer publication ---------------------


def _hold_doc_reads(page):
    """Hold every GET /__data/doc while armed, each with its server answer
    already fetched (the revision current when the read reached the server)."""
    ctl = {"armed": True, "holds": []}

    def hold(route):
        if not ctl["armed"] or route.request.method != "GET":
            return route.continue_()
        ctl["holds"].append((route, route.fetch()))

    page.route("**/__data/doc*", hold)
    return ctl


def _write_marker(bundle, marker):
    _cli("replace-text", "--bundle", str(bundle), "--id", TARGET,
         "--old", "Second list item", "--new", f"Second list item {marker}")


def test_cold_boot_comments_reader_cannot_roll_back_first_publication(studio, page):
    """S1: the boot comments load and live's first reconciliation both read
    revision A; B is published first by an unheld live patch. Releasing the
    held A answers must not roll the page back to A."""
    base, bundle = studio
    ctl = _hold_doc_reads(page)
    page.goto(f"{base}/{TARGET}", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    _wait_for(page, lambda: len(ctl["holds"]) >= 2, "both boot document reads")
    assert page.evaluate("() => window.okfLoomStudio.state.doc") is None

    ctl["armed"] = False
    _write_marker(bundle, "S1-MARKER-B")
    body = page.locator(".okf-page__body")
    expect(body).to_contain_text("S1-MARKER-B", timeout=15000)
    rev_b = _rev(page)
    assert rev_b and page.evaluate("() => window.okfLoomStudio.state.comments.length === 0")

    for route, response in ctl["holds"]:
        route.fulfill(response=response)
    assert _settled_comments(page) == "ready"
    assert _rev(page) == rev_b, "a stale boot read rolled the first publication back"
    expect(body).to_contain_text("S1-MARKER-B")
    assert page.evaluate("(id) => window.okfLoomLive._getLastDocRevForTest(id)", TARGET) == rev_b
    page.unroute("**/__data/doc*")


def test_capture_canonical_read_cannot_roll_back_first_publication(studio, page):
    """ensureCanonicalDoc: a text capture started before the first publication
    reads revision A; B commits first. The capture uses the committed document
    and never commits A over it."""
    base, bundle = studio
    ctl = _hold_doc_reads(page)
    page.goto(f"{base}/{TARGET}", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    _wait_for(page, lambda: len(ctl["holds"]) >= 2, "both boot document reads")
    _select_text(page, ".okf-page__body", "Alpha paragraph")
    page.evaluate("""() => { window.__q4Capture = window.okfLoomStudio._captureCanonicalAnchor().then(
      (a) => ({ ok: true, exact: a.exact }), (e) => ({ ok: false, name: e.name, message: e.message })); }""")
    _wait_for(page, lambda: len(ctl["holds"]) >= 3, "the capture's canonical read")
    boot, capture = ctl["holds"][:2], ctl["holds"][2]

    ctl["armed"] = False
    _write_marker(bundle, "CAPTURE-MARKER-B")
    body = page.locator(".okf-page__body")
    expect(body).to_contain_text("CAPTURE-MARKER-B", timeout=15000)
    rev_b = _rev(page)
    for route, response in boot:
        route.fulfill(response=response)
    assert _settled_comments(page) == "ready"
    assert _rev(page) == rev_b

    capture[0].fulfill(response=capture[1])
    result = page.evaluate("() => window.__q4Capture")
    assert _rev(page) == rev_b, ("the capture's stale read rolled the page back", result)
    expect(body).to_contain_text("CAPTURE-MARKER-B")
    assert result == {"ok": True, "exact": "Alpha paragraph"}, result
    page.unroute("**/__data/doc*")


def test_held_live_patch_cannot_overwrite_newer_commit(studio, page):
    """S2: a live patch read held at A must not commit over a newer commit of
    a different revision; it re-reads the canonical document once instead."""
    base, _bundle = studio
    _boot(page, base)
    rev_a = _rev(page)
    ctl = _hold_doc_reads(page)
    page.evaluate("() => { void window.okfLoomLive.patchNow(); }")
    _wait_for(page, lambda: len(ctl["holds"]) >= 1, "the held live patch read")

    _publish_newer_revision(page)
    body = page.locator(".okf-page__body")
    expect(body).to_contain_text("NEWER-COMMIT")

    stale_route, stale_response = ctl["holds"][0]
    stale_route.fulfill(response=stale_response)
    _wait_for(page, lambda: len(ctl["holds"]) >= 2 or _rev(page) != NEWER_REV,
              "the live patch to re-read or commit")
    assert _rev(page) == NEWER_REV, "the held patch committed its stale revision"
    expect(body).to_contain_text("NEWER-COMMIT")
    assert len(ctl["holds"]) == 2, "the superseded live patch re-reads the canonical document"

    # The re-read is the canonical answer (the server really is at A).
    ctl["armed"] = False
    reread_route, reread_response = ctl["holds"][1]
    reread_route.fulfill(response=reread_response)
    page.wait_for_function("(rev) => window.okfLoomStudio.state.doc.concept_content_rev === rev", arg=rev_a)
    expect(body).not_to_contain_text("NEWER-COMMIT")
    assert page.evaluate("(id) => window.okfLoomLive._getLastDocRevForTest(id)", TARGET) == rev_a
    page.unroute("**/__data/doc*")


# ---- (b) inline reply and edit ---------------------------------------------


def _hold_first_reply(page, parent_id, persist=True):
    held = {}

    def hold(route):
        request = route.request
        if ("route" not in held and request.method == "POST"
                and json.loads(request.post_data).get("parent_id") == parent_id):
            held["key"] = request.headers.get("idempotency-key")
            held["response"] = route.fetch() if persist else None  # the REAL server persists it
            held["route"] = route
            return
        route.continue_()

    page.route("**/__comment", hold)
    return held


def _open_reply(page, root_id):
    page.locator(f"#comment-{root_id}").get_by_role("button", name="Reply", exact=True).first.click()
    return page.get_by_role("textbox", name="Reply to comment")


def test_inline_reply_text_typed_after_dispatch_survives_success(studio, page):
    """Nothing can be typed into a reply while it is sending, so a success
    that closes the reply box cannot drop later text."""
    base, bundle = studio
    _boot(page, base)
    root = _post_root(page, bundle, "Root for the reply lock.")
    held = _hold_first_reply(page, root["id"])
    box = _open_reply(page, root["id"])
    box.fill("first")
    box.press("Enter")
    _wait_for(page, lambda: "route" in held, "the held reply POST")

    reply = page.locator(".okf-inline-reply")
    expect(box).to_be_disabled()
    expect(box).to_have_value("first")
    expect(reply.get_by_role("button", name="Sending…", exact=True)).to_be_disabled()

    held["route"].fulfill(response=held["response"])
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    expect(box).to_have_count(0)
    assert [row["body"] for row in _replies(bundle, root["id"])] == ["first"]
    page.unroute("**/__comment")


def test_inline_reply_edited_after_uncertain_does_not_promise_no_duplicate(studio, page):
    """An unconfirmed reply offers a same-key Retry only for the same text.
    Changing the text drops Retry and warns that the earlier reply may already
    be posted; sending it is a new reply under a new key."""
    base, bundle = studio
    _boot(page, base)
    root = _post_root(page, bundle, "Root for an edited retry.")
    armed = {"on": True}
    keys = []

    def lose_response(route):
        request = route.request
        if request.method != "POST" or json.loads(request.post_data).get("parent_id") != root["id"]:
            return route.continue_()
        keys.append(request.headers.get("idempotency-key"))
        if not armed["on"]:
            return route.continue_()
        route.fetch()  # the REAL server persists the reply
        route.fulfill(status=503, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "injected outage"}))

    page.route("**/__comment", lose_response)
    box = _open_reply(page, root["id"])
    box.fill("Uncertain reply draft.")
    box.press("Enter")
    reply = page.locator(".okf-inline-reply")
    status = reply.locator(".okf-composer__status")
    retry = reply.get_by_role("button", name="Retry reply", exact=True)
    expect(retry).to_be_enabled()
    expect(box).to_be_enabled()
    expect(status).to_have_text(UNCERTAIN_REPLY)
    expect(status).to_have_attribute('data-tone', 'warn')
    assert status.get_attribute('style') is None
    assert status.evaluate("n => getComputedStyle(n).borderRadius") == '0px'
    assert status.evaluate("n => getComputedStyle(n).boxShadow").endswith('2px 0px 0px 0px inset')
    assert [row["body"] for row in _replies(bundle, root["id"])] == ["Uncertain reply draft."]

    box.fill("Changed reply text.")
    expect(retry).to_have_count(0)
    send = reply.get_by_role("button", name="Reply", exact=True)
    expect(send).to_be_enabled()
    expect(status).to_have_text(EDITED_REPLY)

    # The warning, not the old promise, survives a panel round trip.
    page.get_by_role("button", name="Close panel").click()
    _open_comments(page)
    expect(box).to_have_value("Changed reply text.")
    expect(status).to_have_text(EDITED_REPLY)
    expect(retry).to_have_count(0)

    armed["on"] = False
    send.click()
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    assert sorted(row["body"] for row in _replies(bundle, root["id"])) == [
        "Changed reply text.", "Uncertain reply draft."]
    assert keys[-1] and keys[-1] != keys[0], "changed text is a new reply under a new key"
    page.unroute("**/__comment")


def test_inline_reply_interrupted_by_a_page_change_settles_uncertain(studio, page):
    """A reply in flight when the page's revision changes is aborted; it
    settles as unconfirmed (never stuck on Sending…) and a same-text retry
    replays the same key, so the server stores it once."""
    base, bundle = studio
    _boot(page, base)
    root = _post_root(page, bundle, "Root for an interrupted reply.")
    held = _hold_first_reply(page, root["id"])
    box = _open_reply(page, root["id"])
    box.fill("Reply during a page change.")
    box.press("Enter")
    _wait_for(page, lambda: "route" in held, "the held reply POST")
    expect(box).to_be_disabled()

    _publish_newer_revision(page)
    reply = page.locator(".okf-inline-reply")
    retry = reply.get_by_role("button", name="Retry reply", exact=True)
    expect(retry).to_be_enabled()
    expect(box).to_be_enabled()
    expect(box).to_have_value("Reply during a page change.")
    expect(reply.locator(".okf-composer__status")).to_have_text(UNCERTAIN_REPLY)

    page.unroute("**/__comment")
    retry.click()
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    assert [row["body"] for row in _replies(bundle, root["id"])] == ["Reply during a page change."]


def test_inline_edit_text_is_locked_while_saving(studio, page):
    base, bundle = studio
    _boot(page, base)
    root = _post_root(page, bundle, "Root for the edit lock.")
    held = {}

    def hold_update(route):
        if "route" not in held and route.request.method == "POST":
            held["route"] = route  # nothing reaches the server
            return
        route.continue_()

    page.route("**/__comment-update", hold_update)
    page.locator(f"#comment-{root['id']}").get_by_role("button", name="Edit", exact=True).click()
    box = page.get_by_role("textbox", name="Edit comment")
    box.fill("Edited while locked.")
    page.get_by_role("button", name="Save", exact=True).click()
    _wait_for(page, lambda: "route" in held, "the held edit POST")
    expect(box).to_be_disabled()
    expect(page.get_by_role("button", name="Saving…", exact=True)).to_be_disabled()

    held["route"].fulfill(status=500, content_type="application/json",
                          body=json.dumps({"ok": False, "error": "injected outage"}))
    save = page.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_enabled()
    expect(box).to_be_enabled()
    expect(box).to_have_value("Edited while locked.")
    page.unroute("**/__comment-update")
    save.click()
    expect(page.get_by_text("Comment updated.")).to_be_visible()
    assert [row["body"] for row in _directives(bundle)] == ["Edited while locked."]


# ---- (b) main composer anchor while posting --------------------------------


def _hold_first_post(page):
    held = {"keys": []}

    def hold(route):
        request = route.request
        if request.method != "POST":
            return route.continue_()
        held["keys"].append(request.headers.get("idempotency-key"))
        if "route" in held:
            return route.continue_()
        held["response"] = route.fetch()  # the REAL server persists it
        held["route"] = route

    page.route("**/__comment", hold)
    return held


def test_selection_does_not_retarget_a_post_in_flight(studio, page):
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    held = _hold_first_post(page)
    _send(page, "Page comment in flight.")
    _wait_for(page, lambda: "route" in held, "the held comment POST")
    anchor = page.locator(".okf-composer__anchor code")
    expect(anchor).to_have_text(TARGET)

    _select_text(page, ".okf-page__body", "Alpha paragraph")
    action = page.get_by_role("button", name="Comment on the selected text")
    expect(action).to_be_visible()
    action.click()
    expect(page.locator(".okf-toast", has_text="still being posted")).to_be_visible()
    expect(anchor).to_have_text(TARGET)
    expect(page.locator(".okf-page__body mark.okf-comment-mark")).to_have_count(0)

    held["route"].fulfill(response=held["response"])
    _expect_posted(page)
    expect(anchor).to_have_text(TARGET)
    expect(page.locator(".okf-page__body mark.okf-comment-mark")).to_have_count(0)
    [stored] = _directives(bundle)
    assert stored["anchor"] == PAGE_ANCHOR and stored["body"] == "Page comment in flight."
    page.unroute("**/__comment")


# ---- (d) main composer settlement ------------------------------------------


def test_composer_post_losing_ownership_settles_not_pending(studio, page):
    """The page's revision changes while a page comment is posting (the agent
    edits it): the aborted post settles as unconfirmed with its text back, and
    Retry send replays the same key."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    held = _hold_first_post(page)
    _send(page, "Posted while the page changed.")
    _wait_for(page, lambda: "route" in held, "the held comment POST")
    status = page.locator(".okf-composer__status")
    expect(status).to_have_text("Posting comment…")

    _publish_newer_revision(page)
    _composer_settled(page)
    box = page.get_by_role("textbox", name="Comment for agent")
    retry = page.get_by_role("button", name="Retry send", exact=True)
    expect(box).to_be_enabled()
    expect(box).to_have_value("Posted while the page changed.")
    expect(status).to_have_text(UNCERTAIN_COMMENT)
    expect(retry).to_be_enabled()
    expect(page.locator(".okf-composer")).not_to_have_attribute("aria-busy", "true")
    assert page.evaluate("() => window.okfLoomStudio.state.composerPost.outcome") == "uncertain"

    page.unroute("**/__comment")
    keys = []
    page.on("request", lambda r: keys.append(r.headers.get("idempotency-key"))
            if r.method == "POST" and r.url.endswith("/__comment") else None)
    retry.click()
    _expect_posted(page)
    expect(box).to_have_value("")
    assert keys == [held["keys"][0]], "the retry replays the aborted post's key"
    assert [row["body"] for row in _directives(bundle)] == ["Posted while the page changed."]


def test_composer_post_whose_refresh_loses_ownership_still_finishes(studio, page):
    """The post is accepted, then its confirming refresh is overtaken by a
    newer revision: the confirmed row is already listed, so the post finishes
    as posted instead of staying on "Posting comment…"."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    ctl = {"armed": False, "holds": []}

    def hold_refreshes(route):
        if not ctl["armed"]:
            return route.continue_()
        ctl["holds"].append(route)

    def accept_then_hold_refreshes(route):
        if route.request.method != "POST":
            return route.continue_()
        response = route.fetch()  # the REAL server persists it
        ctl["armed"] = True  # every comments refresh from here on is held
        route.fulfill(response=response)

    page.route("**/__comments*", hold_refreshes)
    page.route("**/__comment", accept_then_hold_refreshes)
    posted = []
    page.on("response", lambda r: posted.append(r.status)
            if r.request.method == "POST" and r.url.endswith("/__comment") else None)
    _send(page, "Accepted before the page changed.")
    _wait_for(page, lambda: posted == [201], "the accepted POST")
    _wait_for(page, lambda: len(ctl["holds"]) >= 1, "the held confirming refresh")

    # The page changes while the confirming refresh runs.
    _publish_newer_revision(page)
    ctl["armed"] = False
    for route in ctl["holds"]:
        try:
            route.continue_()
        except Exception:  # the page change already aborted this refresh
            pass
    _composer_settled(page)
    _expect_posted(page)
    box = page.get_by_role("textbox", name="Comment for agent")
    expect(box).to_be_enabled()
    expect(box).to_have_value("")
    expect(page.locator(".okf-composer__status")).to_have_count(0)
    expect(page.locator(".okf-composer")).not_to_have_attribute("aria-busy", "true")
    assert page.evaluate("() => window.okfLoomStudio.state.composerPost") is None
    assert [row["body"] for row in _directives(bundle)] == ["Accepted before the page changed."]
    page.unroute("**/__comments*")
    page.unroute("**/__comment")


def test_cancelled_post_late_success_keeps_the_newer_graph_draft(graph_studio, page):
    """R4 D2: Cancel abandons a pending post. Its late success is stored on
    the concept it was sent to and only updates the comments list: the newer
    draft typed for another concept keeps its text and target."""
    base, bundle = graph_studio
    page.goto(f"{base}/__graph?selected=sub/one", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    assert _settled_comments(page) == "ready"
    page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="attached")
    held = {}

    def hold_post(route):
        if route.request.method != "POST" or "route" in held:
            return route.continue_()
        held["route"] = route  # nothing reaches the server until released

    # Comment reads are held once the late post is released, so only the
    # post's own result can list its row.
    reads = {"armed": False, "holds": []}

    def hold_reads(route):
        if not reads["armed"] or route.request.method != "GET":
            return route.continue_()
        reads["holds"].append(route)

    page.route("**/__comment", hold_post)
    page.route("**/__comments*", hold_reads)
    page.get_by_role("button", name="Comment on this concept", exact=True).click()
    box = page.get_by_role("textbox", name="Comment for agent")
    anchor = page.locator(".okf-composer__anchor code")
    box.fill("About one")
    expect(anchor).to_have_text("sub/one")
    page.get_by_role("button", name="Send", exact=True).click()
    _wait_for(page, lambda: "route" in held, "the held comment POST")
    composer = page.locator(".okf-composer")
    expect(composer).to_have_attribute("aria-busy", "true")

    page.locator(".okf-composer__cancel").click()
    # The abandoned attempt no longer owns the composer: it is not busy.
    expect(composer).not_to_have_attribute("aria-busy", "true")
    expect(box).to_be_enabled()
    expect(box).to_have_value("")
    page.evaluate("() => window.okfActivateGraphConcept('sub/two', 'keyboard')")
    page.wait_for_selector('#detail-body[data-concept-id="sub/two"]', state="visible")
    expect(anchor).to_have_text("sub/two")
    box.fill("About two")

    reads["armed"] = True
    held["route"].continue_()
    # The late confirmed row only joins the comments list.
    page.wait_for_function("""() => { const rows = window.okfLoomStudio.state.comments;
      return rows.some((c) => c.body === 'About one' && !c._posting) && !rows.some((c) => c._posting); }""")
    expect(box).to_have_value("About two")
    expect(anchor).to_have_text("sub/two")
    expect(page.locator("#okf-annotation-status")).not_to_have_text("Comment posted.")
    assert page.evaluate("() => window.okfLoomStudio.state.composerPost") is None
    [stored] = _directives(bundle)
    assert (stored["body"], stored["concept"]) == ("About one", "sub/one")
    reads["armed"] = False
    for route in reads["holds"]:
        try:
            route.continue_()
        except Exception:  # a newer comments load already superseded this one
            pass
    page.unroute("**/__comments*")

    page.unroute("**/__comment")
    page.get_by_role("button", name="Send", exact=True).click()
    _expect_posted(page)
    rows = {row["body"]: row["concept"] for row in _directives(bundle)}
    assert rows == {"About one": "sub/one", "About two": "sub/two"}


# ---- follow-ups: no duplicate comment, no silently lost text --------------


def _anchor_selection(page, exact):
    _select_text(page, ".okf-page__body", exact)
    page.get_by_role("button", name="Comment on the selected text").click()
    page.wait_for_function(
        "(exact) => { const a = window.okfLoomStudio.state.draftAnchor;"
        " return !!a && a.kind === 'text-quote' && a.exact === exact; }", arg=exact)


def _lose_post_responses(page, status=500, body=None):
    """Every POST /__comment reaches the REAL server (which stores it) while
    armed, and its answer is replaced; returns the control with the keys sent."""
    ctl = {"armed": True, "keys": []}

    def lose(route):
        request = route.request
        if request.method != "POST":
            return route.continue_()
        ctl["keys"].append(request.headers.get("idempotency-key"))
        if not ctl["armed"]:
            return route.continue_()
        route.fetch()
        route.fulfill(status=status, content_type="application/json",
                      body=body if body is not None else json.dumps({"ok": False, "error": "injected outage"}))

    page.route("**/__comment", lose)
    return ctl


def _composer_status(page):
    return page.locator(".okf-composer > .okf-composer__status")


def test_retry_refresh_after_revision_change_never_reposts(studio, page):
    """An accepted post whose confirming refresh failed is only re-checked. A
    newer revision resets its exact anchor meanwhile, and Retry refresh still
    re-reads the comments without a second POST."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _anchor_selection(page, "Alpha paragraph")
    failing = {"on": False}
    posts = []

    def fail_refreshes(route):
        if failing["on"] and route.request.method == "GET":
            return route.fulfill(status=500, content_type="application/json",
                                 body=json.dumps({"ok": False, "error": "injected outage"}))
        route.continue_()

    def accept_then_fail_refreshes(route):
        if route.request.method != "POST":
            return route.continue_()
        posts.append(route.request.headers.get("idempotency-key"))
        response = route.fetch()  # the REAL server persists it
        failing["on"] = True
        route.fulfill(response=response)

    page.route("**/__comments*", fail_refreshes)
    page.route("**/__comment", accept_then_fail_refreshes)
    page.evaluate("() => { window.okfLoomStudio.state.annotationRetryDelays = [0, 0]; }")
    _send(page, "Accepted, then the page changed.")
    retry = page.get_by_role("button", name="Retry refresh", exact=True)
    expect(retry).to_be_enabled()
    assert page.evaluate("() => window.okfLoomStudio.state.composerPost.outcome") == "accepted-refresh-failed"

    _publish_newer_revision(page)
    assert page.evaluate("() => window.okfLoomStudio.state.draftAnchor.kind") != "text-quote"
    expect(retry).to_be_enabled()
    failing["on"] = False
    retry.click()
    _expect_posted(page)
    assert len(posts) == 1, ("Retry refresh posted the comment again", posts)
    assert [row["body"] for row in _directives(bundle)] == ["Accepted, then the page changed."]
    page.unroute("**/__comments*")
    page.unroute("**/__comment")


def test_unconfirmed_post_keeps_its_key_while_the_draft_is_the_sent_one(studio, page):
    """An unconfirmed post keeps its key while the draft is the one that was
    sent: a new selection is refused, a typed character turns Retry send into
    Send with a warning, and deleting it brings Retry send back. The retry
    replays the key, so the server stores the comment once."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    lost = _lose_post_responses(page)
    _send(page, "Stored but unconfirmed.")
    box = page.get_by_role("textbox", name="Comment for agent")
    status = _composer_status(page)
    retry = page.get_by_role("button", name="Retry send", exact=True)
    send = page.get_by_role("button", name="Send", exact=True)
    expect(retry).to_be_enabled()
    expect(status).to_have_text(UNCERTAIN_COMMENT)

    anchor = page.locator(".okf-composer__anchor code")
    _select_text(page, ".okf-page__body", "Alpha paragraph")
    page.get_by_role("button", name="Comment on the selected text").click()
    expect(page.locator(".okf-toast", has_text="could not be confirmed")).to_be_visible()
    expect(anchor).to_have_text(TARGET)
    expect(page.locator(".okf-page__body mark.okf-comment-mark")).to_have_count(0)

    box.press("End")
    box.press("x")
    expect(send).to_be_enabled()
    expect(retry).to_have_count(0)
    expect(status).to_have_text(CHANGED_COMMENT)
    box.press("Backspace")
    expect(retry).to_be_enabled()
    expect(status).to_have_text(UNCERTAIN_COMMENT)

    lost["armed"] = False
    retry.click()
    _expect_posted(page)
    assert len(lost["keys"]) == 2 and lost["keys"][0] == lost["keys"][1], lost["keys"]
    assert [row["body"] for row in _directives(bundle)] == ["Stored but unconfirmed."]
    page.unroute("**/__comment")


def test_unconfirmed_exact_comment_after_a_page_change_is_sent_as_new(studio, page):
    """A newer revision drops the exact anchor of an unconfirmed post, so the
    draft is no longer the one that was sent: Send (not Retry send) with a
    warning that the earlier comment may already be posted, under a new key."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _anchor_selection(page, "Alpha paragraph")
    lost = _lose_post_responses(page)
    _send(page, "Exact comment, unconfirmed.")
    status = _composer_status(page)
    retry = page.get_by_role("button", name="Retry send", exact=True)
    send = page.get_by_role("button", name="Send", exact=True)
    expect(retry).to_be_enabled()
    expect(status).to_have_text(UNCERTAIN_COMMENT)

    # A real newer revision (the agent edits the page), so no later canonical
    # read changes the revision under the new post.
    _write_marker(bundle, "EXACT-NEWER")
    expect(page.locator(".okf-page__body")).to_contain_text("EXACT-NEWER", timeout=15000)
    expect(page.locator(".okf-composer__anchor code")).to_have_text(TARGET)
    expect(send).to_be_enabled()
    expect(retry).to_have_count(0)
    expect(status).to_have_text(CHANGED_COMMENT)

    lost["armed"] = False
    send.click()
    _expect_posted(page)
    assert len(lost["keys"]) == 2 and lost["keys"][0] != lost["keys"][1], lost["keys"]
    rows = _directives(bundle)
    assert [row["body"] for row in rows] == ["Exact comment, unconfirmed."] * 2
    assert sum(row["anchor"] == PAGE_ANCHOR for row in rows) == 1, rows
    page.unroute("**/__comment")


def test_accepted_post_with_an_unreadable_answer_is_unconfirmed(studio, page):
    """A 2xx whose body cannot be read may have stored the comment: it is
    unconfirmed (Retry send, same key), not rejected under a new key."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    lost = _lose_post_responses(page, status=201, body="{not json")
    _send(page, "Stored, answer unreadable.")
    retry = page.get_by_role("button", name="Retry send", exact=True)
    expect(retry).to_be_enabled()
    expect(_composer_status(page)).to_have_text(UNCERTAIN_COMMENT)

    lost["armed"] = False
    retry.click()
    _expect_posted(page)
    assert len(lost["keys"]) == 2 and lost["keys"][0] == lost["keys"][1], lost["keys"]
    assert [row["body"] for row in _directives(bundle)] == ["Stored, answer unreadable."]
    page.unroute("**/__comment")


def test_in_document_back_does_not_orphan_posts(studio, page):
    """Back within the same page (live.js advances its navigation epoch on
    every popstate) is not a page change: a reply it aborts settles as
    unconfirmed with its text, the composer's post still settles with its text
    back, and both retries store each comment once."""
    base, bundle = studio
    _boot(page, base)
    root = _post_root(page, bundle, "Root for an in-document Back.")
    page.evaluate("() => { window.__q4SamePage = true; history.pushState(null, '', '#q4'); }")
    held = {}

    def hold(route):
        request = route.request
        if request.method != "POST":
            return route.continue_()
        which = "reply" if json.loads(request.post_data).get("parent_id") == root["id"] else "comment"
        if which in held:
            return route.continue_()
        held[which] = route  # nothing reaches the server

    page.route("**/__comment", hold)
    _send(page, "Comment across Back.")
    _wait_for(page, lambda: "comment" in held, "the held comment POST")
    box = _open_reply(page, root["id"])
    box.fill("Reply across Back.")
    box.press("Enter")
    _wait_for(page, lambda: "reply" in held, "the held reply POST")
    expect(box).to_be_disabled()

    page.go_back()
    page.wait_for_function("() => location.hash === ''")
    assert page.evaluate("() => window.__q4SamePage === true"), "Back reloaded the page"
    reply = page.locator(".okf-inline-reply")
    retry_reply = reply.get_by_role("button", name="Retry reply", exact=True)
    expect(retry_reply).to_be_enabled()
    expect(box).to_be_enabled()
    expect(box).to_have_value("Reply across Back.")
    expect(reply.locator(".okf-composer__status")).to_have_text(UNCERTAIN_REPLY)

    held["comment"].fulfill(status=500, content_type="application/json",
                            body=json.dumps({"ok": False, "error": "injected outage"}))
    _composer_settled(page)
    expect(page.get_by_role("textbox", name="Comment for agent")).to_have_value("Comment across Back.")
    expect(_composer_status(page)).to_have_text(UNCERTAIN_COMMENT)
    retry_send = page.get_by_role("button", name="Retry send", exact=True)
    expect(retry_send).to_be_enabled()

    page.unroute("**/__comment")
    retry_reply.click()
    expect(page.get_by_text("Reply posted.")).to_be_visible()
    retry_send.click()
    _expect_posted(page)
    assert [row["body"] for row in _replies(bundle, root["id"])] == ["Reply across Back."]
    assert sorted(row["body"] for row in _directives(bundle)) == [
        "Comment across Back.", "Reply across Back.", "Root for an in-document Back."]


def test_same_revision_recommits_do_not_leave_the_page_behind(studio, page):
    """A re-commit of the revision already shown (the refetch after a comment
    event, lazy Source) is not a publication: it cannot make a live patch's
    read of a newer revision look stale, so the page reaches that revision."""
    base, bundle = studio
    _boot(page, base)
    rev_a = _rev(page)
    page.evaluate("window.okfLoomLive._disconnectForTest()")
    _write_marker(bundle, "MISSED-C")
    ctl = _hold_doc_reads(page)
    page.evaluate("() => { void window.okfLoomLive.patchNow(); }")
    released = 0
    for _ in range(2):
        _wait_for(page, lambda: len(ctl["holds"]) > released, "a held live patch read")
        page.evaluate("""() => { const s = window.okfLoomStudio;
          if (!s.applyDoc(JSON.parse(JSON.stringify(s.state.doc)))) throw new Error('re-commit failed'); }""")
        route, response = ctl["holds"][released]
        released += 1
        route.fulfill(response=response)
        try:
            _wait_for(page, lambda: _rev(page) != rev_a or len(ctl["holds"]) > released,
                      "the read to commit or re-read", timeout_s=5.0)
        except AssertionError:
            break  # the read was dropped
        if _rev(page) != rev_a:
            break
    assert _rev(page) != rev_a, "the page stayed behind the server"
    expect(page.locator(".okf-page__body")).to_contain_text("MISSED-C")
    page.unroute("**/__data/doc*")


def test_capture_after_a_live_patch_rewrote_the_selection_anchors_only_shown_text(studio, page):
    """A live patch of revision B rewrites the block holding the selection. A
    text capture afterwards never anchors text the page no longer shows, and it
    never silently yields no anchor: it asks for the text to be selected again,
    or it anchors text that is still shown."""
    base, bundle = studio
    _boot(page, base)
    _select_text(page, ".okf-page__body", "shared phrase once")
    assert page.evaluate("() => String(window.getSelection())") == "shared phrase once"
    _cli("replace-text", "--bundle", str(bundle), "--id", TARGET,
         "--old", "mentions the shared phrase once", "--new", "mentions a rewritten passage")
    body = page.locator(".okf-page__body")
    expect(body).to_contain_text("a rewritten passage", timeout=15000)
    result = page.evaluate("""() => window.okfLoomStudio._captureCanonicalAnchor().then(
      (a) => ({ ok: true, exact: a ? a.exact : null }), (e) => ({ ok: false, name: e.name, message: e.message }))""")
    if result["ok"]:
        assert result["exact"] and "shared phrase once" not in result["exact"], result
        assert result["exact"] in body.inner_text(), result
    else:
        assert result["message"] == "Select the exact text this comment refers to in the document.", result


def test_inline_edit_save_that_never_answers_unlocks(studio, page):
    """A stalled edit save does not leave the editor read-only on Saving…: at
    its deadline the text is editable again and a warning says the edit could
    not be confirmed."""
    base, bundle = studio
    _boot(page, base)
    root = _post_root(page, bundle, "Root for a stalled edit.")
    held = {}

    def stall(route):
        if "route" not in held and route.request.method == "POST":
            held["route"] = route  # never answered; nothing reaches the server
            return
        route.continue_()

    page.route("**/__comment-update", stall)
    page.evaluate("() => { window.okfLoomStudio.state.annotationDeadlines = { editSave: 500 }; }")
    page.locator(f"#comment-{root['id']}").get_by_role("button", name="Edit", exact=True).click()
    box = page.get_by_role("textbox", name="Edit comment")
    box.fill("Edited after a stall.")
    page.get_by_role("button", name="Save", exact=True).click()
    _wait_for(page, lambda: "route" in held, "the held edit POST")

    save = page.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_enabled()
    expect(box).to_be_enabled()
    expect(box).to_have_value("Edited after a stall.")
    expect(page.locator(".okf-toast", has_text="could not be confirmed")).to_be_visible()
    assert [row["body"] for row in _directives(bundle)] == ["Root for a stalled edit."]

    page.unroute("**/__comment-update")
    save.click()
    expect(page.get_by_text("Comment updated.")).to_be_visible()
    assert [row["body"] for row in _directives(bundle)] == ["Edited after a stall."]


# ---- final round -------------------------------------------------------------


def _outcome(page):
    return page.evaluate("() => { const cp = window.okfLoomStudio.state.composerPost; return cp && cp.outcome; }")


def _fail(route):
    route.fulfill(status=500, content_type="application/json",
                  body=json.dumps({"ok": False, "error": "injected outage"}))


def _control_comment_reads(page):
    """GET /__comments* passes, is held ("hold") or answers 500 ("fail")."""
    ctl = {"mode": "pass", "holds": []}

    def reads(route):
        if route.request.method != "GET" or ctl["mode"] == "pass":
            return route.continue_()
        if ctl["mode"] == "hold":
            ctl["holds"].append(route)
            return
        _fail(route)

    page.route("**/__comments*", reads)
    return ctl


def _fail_held_reads(ctl):
    ctl["mode"] = "fail"
    for route in ctl["holds"]:
        try:
            _fail(route)
        except Exception:  # a newer comments load already superseded this one
            pass


def _push_in_document_entry(page):
    page.evaluate("""() => { window.__q4SamePage = true;
      window.okfLoomStudio.state.annotationRetryDelays = [0, 0];
      history.pushState(null, '', '#q4'); }""")


def _go_back_in_document(page):
    page.go_back()
    page.wait_for_function("() => location.hash === ''")
    assert page.evaluate("() => window.__q4SamePage === true"), "Back reloaded the page"


def _expect_retry_refresh(page):
    retry = page.get_by_role("button", name="Retry refresh", exact=True)
    expect(retry).to_be_enabled()
    expect(_composer_status(page)).to_have_text(ACCEPTED_UNCONFIRMED)
    expect(page.locator(".okf-composer")).not_to_have_attribute("aria-busy", "true")
    expect(page.locator("#okf-annotation-status")).not_to_have_text("Comment posted.")
    assert _outcome(page) == "accepted-refresh-failed"
    return retry


def test_in_document_back_during_failed_confirmation_keeps_retry_refresh(studio, page):
    """Back within the page while an accepted post's confirming refresh runs,
    then the refresh fails: nothing confirmed the post, so the composer offers
    Retry refresh and never says "Comment posted."."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _push_in_document_entry(page)
    reads = _control_comment_reads(page)
    posted = []

    def accept_then_hold_reads(route):
        if route.request.method != "POST":
            return route.continue_()
        response = route.fetch()  # the REAL server persists it
        reads["mode"] = "hold"
        posted.append(response.status)
        route.fulfill(response=response)

    page.route("**/__comment", accept_then_hold_reads)
    _send(page, "Accepted across Back.")
    _wait_for(page, lambda: posted == [201] and reads["holds"], "the held confirming refresh")
    _go_back_in_document(page)
    _fail_held_reads(reads)
    _composer_settled(page)
    retry = _expect_retry_refresh(page)

    reads["mode"] = "pass"
    retry.click()
    _expect_posted(page)
    assert len(posted) == 1
    assert [row["body"] for row in _directives(bundle)] == ["Accepted across Back."]
    page.unroute("**/__comments*")
    page.unroute("**/__comment")


def test_in_document_back_during_failed_retry_refresh_keeps_retry_refresh(studio, page):
    """The same for the re-read Retry refresh starts: Back within the page
    while it runs, then it fails, leaves Retry refresh on offer."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _push_in_document_entry(page)
    reads = _control_comment_reads(page)
    posted = []

    def accept_then_fail_reads(route):
        if route.request.method != "POST":
            return route.continue_()
        response = route.fetch()  # the REAL server persists it
        reads["mode"] = "fail"
        posted.append(response.status)
        route.fulfill(response=response)

    page.route("**/__comment", accept_then_fail_reads)
    _send(page, "Accepted, re-checked across Back.")
    _composer_settled(page)
    retry = _expect_retry_refresh(page)

    reads["mode"] = "hold"
    retry.click()
    _wait_for(page, lambda: reads["holds"], "the held Retry refresh read")
    _go_back_in_document(page)
    _fail_held_reads(reads)
    retry = _expect_retry_refresh(page)

    reads["mode"] = "pass"
    retry.click()
    _expect_posted(page)
    assert posted == [201]
    assert [row["body"] for row in _directives(bundle)] == ["Accepted, re-checked across Back."]
    page.unroute("**/__comments*")
    page.unroute("**/__comment")


def test_graph_post_whose_confirmation_fails_keeps_retry_refresh(graph_studio, page):
    """The graph shows no document revision, so a failed confirming refresh
    there is never taken for a revision change: Retry refresh, not "Comment
    posted."."""
    base, bundle = graph_studio
    page.goto(f"{base}/__graph?selected=sub/one", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    assert _settled_comments(page) == "ready"
    page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="attached")
    page.evaluate("() => { window.okfLoomStudio.state.annotationRetryDelays = [0, 0]; }")
    reads = _control_comment_reads(page)

    def accept_then_fail_reads(route):
        if route.request.method != "POST":
            return route.continue_()
        response = route.fetch()  # the REAL server persists it
        reads["mode"] = "fail"
        route.fulfill(response=response)

    page.route("**/__comment", accept_then_fail_reads)
    page.get_by_role("button", name="Comment on this concept", exact=True).click()
    page.get_by_role("textbox", name="Comment for agent").fill("Accepted on the graph.")
    page.get_by_role("button", name="Send", exact=True).click()
    _composer_settled(page)
    retry = _expect_retry_refresh(page)

    reads["mode"] = "pass"
    retry.click()
    _expect_posted(page)
    [stored] = _directives(bundle)
    assert (stored["body"], stored["concept"]) == ("Accepted on the graph.", "sub/one")
    page.unroute("**/__comments*")
    page.unroute("**/__comment")


def test_stalled_post_body_settles_at_deadline(studio, page):
    """A 2xx whose body never arrives is read within the post's own deadline:
    the post settles as unconfirmed (Retry send) instead of staying on
    "Posting comment…"."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    page.evaluate("""() => { const s = window.okfLoomStudio.state;
      s.annotationDeadlines = { post: 400 };
      s.annotationTransport = (u, req) => Promise.resolve({ ok: true, status: 201,
        json: () => new Promise((_, rej) => req.signal.addEventListener('abort',
          () => rej(new DOMException('a', 'AbortError')), { once: true })) }); }""")
    _send(page, "Answer body never arrives.")
    _composer_settled(page)
    retry = page.get_by_role("button", name="Retry send", exact=True)
    expect(retry).to_be_enabled()
    expect(_composer_status(page)).to_have_text(UNCERTAIN_COMMENT)
    expect(page.locator(".okf-composer")).not_to_have_attribute("aria-busy", "true")
    assert _outcome(page) == "uncertain"

    page.evaluate("""() => { const s = window.okfLoomStudio.state;
      s.annotationTransport = null; s.annotationDeadlines = null; }""")
    retry.click()
    _expect_posted(page)
    assert [row["body"] for row in _directives(bundle)] == ["Answer body never arrives."]


def test_boot_capture_after_a_revision_rewrote_the_selection_anchors_only_shown_text(studio, page):
    """A text capture started before the first publication; revision B then
    rewrites the block holding the selection. The capture keeps B and never
    anchors the text B no longer shows: it asks for the text to be selected
    again, or it anchors text that is still shown."""
    base, bundle = studio
    ctl = _hold_doc_reads(page)
    page.goto(f"{base}/{TARGET}", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    _wait_for(page, lambda: len(ctl["holds"]) >= 2, "both boot document reads")
    _select_text(page, ".okf-page__body", "shared phrase once")
    page.evaluate("""() => { window.__q4Capture = window.okfLoomStudio._captureCanonicalAnchor().then(
      (a) => ({ ok: true, exact: a ? a.exact : null }), (e) => ({ ok: false, name: e.name, message: e.message })); }""")
    _wait_for(page, lambda: len(ctl["holds"]) >= 3, "the capture's canonical read")
    boot, capture = ctl["holds"][:2], ctl["holds"][2]

    ctl["armed"] = False
    _cli("replace-text", "--bundle", str(bundle), "--id", TARGET,
         "--old", "mentions the shared phrase once", "--new", "mentions a REWRITE-B passage")
    body = page.locator(".okf-page__body")
    expect(body).to_contain_text("REWRITE-B", timeout=15000)
    rev_b = _rev(page)
    for route, response in boot:
        route.fulfill(response=response)
    assert _settled_comments(page) == "ready"
    assert _rev(page) == rev_b

    capture[0].fulfill(response=capture[1])
    result = page.evaluate("() => window.__q4Capture")
    assert _rev(page) == rev_b, ("the capture's stale read rolled the page back", result)
    shown = body.inner_text()
    assert "REWRITE-B" in shown and "shared phrase once" not in shown
    if result["ok"]:
        assert result["exact"] and "shared phrase once" not in result["exact"], result
        assert result["exact"] in shown, result
    else:
        assert result["name"] != "AbortError" and "select" in result["message"].lower(), result
    page.unroute("**/__data/doc*")


def test_escape_removing_an_unconfirmed_posts_selection_sends_it_as_new(studio, page):
    """Esc removes the selection from an unconfirmed exact post's draft, so it
    is no longer the sent draft: Send with the warning (never the same-key
    Retry send), and sending it is a new comment under a new key."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    _anchor_selection(page, "Alpha paragraph")
    lost = _lose_post_responses(page)
    _send(page, "Exact, then Esc.")
    status = _composer_status(page)
    retry = page.get_by_role("button", name="Retry send", exact=True)
    send = page.get_by_role("button", name="Send", exact=True)
    expect(retry).to_be_enabled()
    expect(status).to_have_text(UNCERTAIN_COMMENT)

    page.get_by_role("textbox", name="Comment for agent").press("Escape")
    expect(page.locator(".okf-composer__anchor code")).to_have_text(TARGET)
    expect(send).to_be_enabled()
    expect(retry).to_have_count(0)
    expect(status).to_have_text(CHANGED_COMMENT)

    lost["armed"] = False
    send.click()
    _expect_posted(page)
    assert len(lost["keys"]) == 2 and lost["keys"][0] != lost["keys"][1], lost["keys"]
    rows = _directives(bundle)
    assert [row["body"] for row in rows] == ["Exact, then Esc."] * 2
    assert sum(row["anchor"] == PAGE_ANCHOR for row in rows) == 1, rows


def test_cancel_and_dismiss_keep_an_unconfirmed_posts_key(studio, page):
    """Cancel after an unconfirmed post, or Dismiss after an accepted one whose
    refresh failed, keeps the post's key, as an inline reply does: another
    draft is sent as new with the warning, and the same text again is Retry
    send under that key, so the server stores each comment once."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    box = page.get_by_role("textbox", name="Comment for agent")
    status = _composer_status(page)
    retry = page.get_by_role("button", name="Retry send", exact=True)
    send = page.get_by_role("button", name="Send", exact=True)
    cancel = page.locator(".okf-composer__cancel")

    lost = _lose_post_responses(page)
    _send(page, "Unconfirmed, then cancelled.")
    expect(retry).to_be_enabled()
    expect(cancel).to_have_text("Cancel")
    cancel.click()
    expect(box).to_have_value("")
    expect(retry).to_have_count(0)
    box.fill("A different comment.")
    expect(send).to_be_enabled()
    expect(status).to_have_text(CHANGED_COMMENT)
    box.fill("Unconfirmed, then cancelled.")
    expect(retry).to_be_enabled()
    expect(status).to_have_text(UNCERTAIN_COMMENT)
    lost["armed"] = False
    retry.click()
    _expect_posted(page)
    assert len(lost["keys"]) == 2 and lost["keys"][0] == lost["keys"][1], lost["keys"]
    page.unroute("**/__comment")

    page.evaluate("() => { window.okfLoomStudio.state.annotationRetryDelays = [0, 0]; }")
    reads = _control_comment_reads(page)
    keys = []

    def accept_then_fail_reads(route):
        if route.request.method != "POST":
            return route.continue_()
        keys.append(route.request.headers.get("idempotency-key"))
        response = route.fetch()  # the REAL server persists it (or replays its key)
        if len(keys) == 1:
            reads["mode"] = "fail"  # only the first post's confirmation fails
        route.fulfill(response=response)

    page.route("**/__comment", accept_then_fail_reads)
    _send(page, "Accepted, then dismissed.")
    _composer_settled(page)
    _expect_retry_refresh(page)
    expect(cancel).to_have_text("Dismiss")
    cancel.click()
    expect(box).to_be_enabled()
    expect(box).to_have_value("")
    reads["mode"] = "pass"
    box.fill("Accepted, then dismissed.")
    expect(retry).to_be_enabled()
    retry.click()
    _expect_posted(page)
    assert len(keys) == 2 and keys[0] == keys[1], keys
    assert sorted(row["body"] for row in _directives(bundle)) == [
        "Accepted, then dismissed.", "Unconfirmed, then cancelled."]
    page.unroute("**/__comments*")
    page.unroute("**/__comment")


def test_quick_action_does_not_replace_a_draft_being_posted(studio, page):
    """A Studio home quick action starts a new draft, but while a post is
    pending, or unconfirmed with its sent draft, it is refused the way a new
    selection is: the sent text stays and its retry keeps the key. With no
    post held it fills the composer."""
    base, bundle = studio
    _boot(page, base)
    _open_comments(page)
    held = {"keys": []}

    def hold_then_lose(route):
        request = route.request
        if request.method != "POST":
            return route.continue_()
        held["keys"].append(request.headers.get("idempotency-key"))
        if "route" in held:
            return route.continue_()
        held["route"] = route  # nothing reaches the server

    page.route("**/__comment", hold_then_lose)
    _send(page, "Sent before a quick action.")
    _wait_for(page, lambda: "route" in held, "the held comment POST")
    page.locator(".okf-studio-launcher").click()
    action = page.get_by_role("button", name="Draft a new section", exact=True)
    action.click()
    expect(page.locator(".okf-toast", has_text="still being posted")).to_be_visible()
    expect(action).to_be_visible()

    _fail(held["route"])
    _composer_settled(page)
    assert _outcome(page) == "uncertain"
    action.click()
    expect(page.locator(".okf-toast", has_text="could not be confirmed")).to_be_visible()

    # Studio home is still open: go to its Comments section.
    page.locator("#okf-panel").get_by_role("button", name="Comments").click()
    box = page.get_by_role("textbox", name="Comment for agent")
    expect(box).to_have_value("Sent before a quick action.")
    retry = page.get_by_role("button", name="Retry send", exact=True)
    expect(retry).to_be_enabled()
    retry.click()
    _expect_posted(page)
    assert len(held["keys"]) == 2 and held["keys"][0] == held["keys"][1], held["keys"]
    assert [row["body"] for row in _directives(bundle)] == ["Sent before a quick action."]

    page.locator(".okf-studio-launcher").click()
    action.click()
    expect(box).to_have_value("Draft a new section about ")
    page.unroute("**/__comment")
