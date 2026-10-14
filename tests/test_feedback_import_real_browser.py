"""Real browser proof for the local portable-feedback import page (X2).

Every test drives a real ``serve`` process (or the production handler behind
its real public-ingress listener) and real note files chosen through the file
input. No product response is mocked or altered: one test holds a real preview
response through route interception and then delivers it unchanged.
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from browser_support import expect, live_server
from test_studio_feedback_real_browser import (  # noqa: F401 (fixtures)
    _okf, _rows, _studio_ready, comment_only_base, linked_bundle, page)

pytestmark = pytest.mark.browser

IMPORT = "/__feedback-import"
ORIGIN_COPY = ("Origin is unauthenticated: this file could have been written or edited by anyone. "
               "Byte-identical copies of this page are indistinguishable, so we can't tell which copy "
               "it came from. Name and time are unverified claims.")


def _confirm_copy(document):
    return (f"Importing creates an ordinary open comment on {document}, recorded as you (the local "
            "Studio user). It is visible to every reader and agent of this bundle and may trigger agent work."
            " It is a page comment: any quote is shown with it but is not highlighted or pinned to exact text.")


def _note(bundle, document, *, note_id="0123456789abcdef0123456789abcdef", body="Please link the log.",
          **extra) -> bytes:
    source = (bundle / document).read_bytes() if (bundle / document).exists() else b"gone"
    value = {"format": "okf-loom.portable-feedback", "version": 1, "note_id": note_id,
             "target": {"document": document, "source_sha256": hashlib.sha256(source).hexdigest()},
             "body": body, **extra}
    return json.dumps(value, indent=2).encode("utf-8")


def _choose(page, name, data):
    page.locator("#okf-fbi-file").set_input_files(
        {"name": name, "mimeType": "application/json", "buffer": data})


def _heading(page):
    return page.locator("#okf-fbi-result-title")


def _import_page(page, base):
    page.goto(base + IMPORT, wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    expect(page.get_by_role("heading", level=1, name="Import a feedback note")).to_be_visible()


def _import_requests(page):
    seen = []
    page.on("request", lambda r: seen.append((r.method, r.url.split("/", 3)[-1]))
            if "/__feedback-import/" in r.url else None)
    return seen


def test_operator_previews_imports_and_the_card_keeps_its_provenance(linked_bundle, page):
    with live_server(linked_bundle) as base:
        # The Studio home links the local operator to the page.
        _studio_ready(page, base + "/sub/one")
        page.locator(".okf-studio-launcher").click()
        link = page.get_by_role("link", name="Import feedback note")
        expect(link).to_be_visible()
        link.click()
        page.wait_for_url(base + IMPORT)
        _import_page(page, base)
        requests = _import_requests(page)

        raw = _note(linked_bundle, "sub/one.md", body="The log link is missing.\nPlease add it.",
                    quote="dir-log-link", claimed_author="Ana", claimed_at="2026-09-20T10:15:00Z")
        _choose(page, "sub-one-01234567.okf-feedback.json", raw)
        expect(_heading(page)).to_have_text("Ready to import")
        expect(_heading(page)).to_be_focused()
        result = page.locator("#okf-fbi-result")
        expect(result.locator("[data-fbi-disclosure='origin']")).to_have_text(ORIGIN_COPY)
        expect(result.locator("[data-fbi-disclosure='origin'] strong")).to_have_text("unauthenticated")
        expect(result.locator("[data-fbi-disclosure='confirm']")).to_have_text(_confirm_copy("sub/one.md"))
        expect(result.locator(".okf-fbi__facts")).to_contain_text("sub/one.md")
        expect(result.locator(".okf-fbi__facts")).to_contain_text("Ana unverified")
        assert result.locator('.okf-fbi__facts').evaluate("""facts => {
          const cells = [...facts.children];
          return cells.filter(n => n.tagName === 'DT').every(n => {
            const next = n.nextElementSibling, a = n.getBoundingClientRect(), b = next.getBoundingClientRect();
            return Math.abs(a.right - b.left) < 1 && Math.abs(a.bottom - b.bottom) < 1;
          });
        }"""), 'fact rows must have continuous hairlines between label and value'
        expect(result.locator(".okf-fbi__body")).to_have_text("The log link is missing.\nPlease add it.")
        expect(result.locator("blockquote")).to_have_text("dir-log-link")
        # R3: the quote is the note's own unverified claim, not "from the page".
        expect(result.get_by_role("heading", name="Quote in the note (unverified)", exact=True)).to_be_visible()
        expect(result).not_to_contain_text("Quoted from the page")
        assert requests == [("POST", "__feedback-import/preview")]
        assert _rows(page, base) == []  # preview never creates anything

        page.get_by_role("button", name="Import as comment").click()
        expect(_heading(page)).to_have_text("Imported")
        expect(_heading(page)).to_be_focused()
        expect(result.locator("[data-fbi-message='imported']")).to_have_text(
            "The note is now an open comment on sub/one.md.")
        assert requests == [("POST", "__feedback-import/preview"), ("POST", "__feedback-import/confirm")]
        [row] = _rows(page, base)
        assert (row["actor"], row["state"], row["anchor"], row["detail"]) == ("user", "open", {"kind": "page"}, {})
        assert row["portable_import"]["claimed_author"] == "Ana"

        # Show the comment opens the real Comments panel on the new card.
        page.get_by_role("button", name="Show the comment").click()
        card = page.locator(f"#comment-{row['id']}")
        expect(card).to_be_visible()
        expect(card.locator(".okf-comment__import-chip")).to_have_text("Imported portable note")
        claims = card.locator(".okf-comment__import-claims")
        expect(claims).to_contain_text("Note author (unverified): Ana · Note time (unverified): ")
        expect(claims.locator("time")).to_have_attribute("datetime", "2026-09-20T10:15:00Z")
        # Never phrased like the lifecycle state "claimed by agent".
        expect(claims).not_to_contain_text("claimed")
        expect(card.locator(".okf-comment__import-quote")).to_have_text("dir-log-link")
        expect(card.locator(".okf-comment__import-quote-label")).to_have_text(
            "Quote in the note (unverified) \u2014 not highlighted on the page")
        expect(card.locator(".okf-anchor-row__cause")).to_have_text("About the whole page; no text is highlighted.")

        # On the target page the chip survives an external SSE-driven update.
        _studio_ready(page, base + "/sub/one")
        page.get_by_role("group", name="Page feedback").get_by_role(
            "button", name="1 open comment on this page").click()
        card = page.locator(f"#comment-{row['id']}")
        expect(card.locator(".okf-comment__import-chip")).to_be_visible()
        _okf(linked_bundle, "comment-claim", str(linked_bundle), row["id"], "--no-presence")
        expect(card.locator(".okf-comment__state")).to_have_text("claimed by agent")
        expect(card.locator(".okf-comment__import-chip")).to_have_text("Imported portable note")
        expect(card.locator(".okf-comment__import-claims")).to_contain_text("Note author (unverified): Ana")
    assert page.failed_documents == []


def test_blocked_states_explain_themselves_and_offer_the_next_step(linked_bundle, page):
    with live_server(linked_bundle) as base:
        _import_page(page, base)
        requests = _import_requests(page)
        result = page.locator("#okf-fbi-result")

        _choose(page, "broken.okf-feedback.json", b"{not json")
        expect(_heading(page)).to_have_text("Can't import this note")
        expect(result.locator("[data-fbi-message='invalid']")).to_have_text(
            "This file isn't a valid feedback note: it isn't valid JSON.")
        expect(page.get_by_role("button", name="Import as comment")).to_have_count(0)

        # Oversize is refused locally without sending the bytes anywhere.
        before = len(requests)
        _choose(page, "huge.okf-feedback.json", b" " * (32 * 1024 + 1))
        expect(result.locator("[data-fbi-message='too_large']")).to_be_visible()
        assert len(requests) == before

        _choose(page, "missing.okf-feedback.json", _note(linked_bundle, "sub/missing.md", note_id="1" * 32))
        expect(result.locator("[data-fbi-message='target_missing']")).to_have_text("The page no longer exists.")

        raw = _note(linked_bundle, "sub/two.md", note_id="2" * 32)
        _choose(page, "two.okf-feedback.json", raw)
        expect(_heading(page)).to_have_text("Ready to import")
        page.get_by_role("button", name="Import as comment").click()
        expect(_heading(page)).to_have_text("Imported")
        [row] = _rows(page, base)

        # The same file again: nothing to import, with a way to the comment.
        _choose(page, "two-again.okf-feedback.json", raw)
        expect(_heading(page)).to_have_text("Nothing to import")
        expect(result.locator("[data-fbi-message='already_imported']")).to_have_text("Already imported.")
        expect(result.get_by_role("link", name="Open sub/two.md")).to_have_attribute("href", "/sub/two")
        page.get_by_role("button", name="Show the comment").click()
        expect(page.locator(f"#comment-{row['id']} .okf-comment__import-chip")).to_be_visible()
        page.keyboard.press("Escape")

        # Same id, different words: a conflict, never a second comment.
        _choose(page, "two-edited.okf-feedback.json", _note(linked_bundle, "sub/two.md", note_id="2" * 32,
                                                              body="Edited words"))
        expect(result.locator("[data-fbi-message='conflict']")).to_have_text(
            "A different note with this ID was already imported, or the ID has expired from the duplicate check.")
        assert len(_rows(page, base)) == 1
    assert page.failed_documents == []


def test_a_page_change_makes_the_preview_stale_and_confirm_is_withdrawn(linked_bundle, page):
    with live_server(linked_bundle) as base:
        _import_page(page, base)
        page.wait_for_function("() => window.okfLoomLive && window.okfLoomLive.state === 'online'")
        result = page.locator("#okf-fbi-result")
        _choose(page, "two.okf-feedback.json", _note(linked_bundle, "sub/two.md"))
        expect(_heading(page)).to_have_text("Ready to import")
        page.get_by_role("button", name="Import as comment").focus()

        two = linked_bundle / "sub" / "two.md"
        two.write_bytes(two.read_bytes() + b"\nA later paragraph.\n")
        expect(_heading(page)).to_have_text("Preview out of date", timeout=15000)
        stale = result.locator("[data-fbi-message='stale']")
        expect(stale).to_contain_text("The page or session changed; preview again.")
        expect(stale).to_contain_text("The target page was updated.")
        expect(page.get_by_role("button", name="Import as comment")).to_have_count(0)
        # The control under focus was withdrawn: focus moves to its replacement.
        expect(page.get_by_role("button", name="Preview again")).to_be_focused()
        expect(page.locator("#okf-fbi-live")).to_contain_text("The page or session changed; preview again.")

        page.keyboard.press("Enter")
        expect(_heading(page)).to_have_text("Can't import this note")
        expect(result.locator("[data-fbi-message='revision_mismatch']")).to_have_text(
            "The page has changed since this note was written; it can't be imported.")
        expect(result.get_by_role("link", name="Open sub/two.md")).to_be_visible()
        assert _rows(page, base) == []

        # A live resync is also a gap in what this page observed.
        _choose(page, "one.okf-feedback.json", _note(linked_bundle, "sub/one.md", note_id="3" * 32))
        expect(_heading(page)).to_have_text("Ready to import")
        page.evaluate("() => window.okfLoomLive.emit('resync', {doc_revs: null})")
        expect(result.locator("[data-fbi-message='stale']")).to_contain_text("resynchronised")
        page.get_by_role("button", name="Preview again").click()
        expect(_heading(page)).to_have_text("Ready to import")
        page.get_by_role("button", name="Import as comment").click()
        expect(_heading(page)).to_have_text("Imported")
        assert len(_rows(page, base)) == 1


def _hold_previews(page):
    """Park every preview request until the test delivers its real response."""
    held = []
    page.route("**" + IMPORT + "/preview", lambda route: held.append(route))
    return held


def _parked(page, held, count):
    # Route handlers run while Playwright pumps events; a cheap round trip to
    # the page pumps them until the chosen file's preview request is parked.
    deadline = time.monotonic() + 10
    while len(held) < count:
        assert time.monotonic() < deadline, "the preview request was never sent"
        page.evaluate("() => 0")
    return held[count - 1]


def _live_ids_seen(page):
    page.evaluate("""() => { window.__fbiSeen = [];
      ['changed', 'created', 'removed'].forEach(type => window.okfLoomLive.on(type,
        d => window.__fbiSeen.push(...((d && Array.isArray(d.ids)) ? d.ids : [])))); }""")


def test_a_target_change_while_the_first_preview_is_in_flight_is_not_lost(linked_bundle, page):
    with live_server(linked_bundle) as base:
        _import_page(page, base)
        page.wait_for_function("() => window.okfLoomLive && window.okfLoomLive.state === 'online'")
        _live_ids_seen(page)
        held = _hold_previews(page)
        result = page.locator("#okf-fbi-result")

        # A change to a different page during the first preview leaves it usable.
        _choose(page, "one.okf-feedback.json", _note(linked_bundle, "sub/one.md"))
        expect(_heading(page)).to_have_text("Checking the note…")
        route = _parked(page, held, 1)
        response = route.fetch()  # the server reads sub/one.md now, and it matches
        assert response.status == 200 and response.json()["status"] == "ready"
        top = linked_bundle / "top.md"
        top.write_bytes(top.read_bytes() + b"\nAn unrelated paragraph.\n")
        page.wait_for_function("() => window.__fbiSeen.includes('top')", timeout=15000)
        expect(_heading(page)).to_have_text("Checking the note…")
        route.fulfill(response=response)
        expect(_heading(page)).to_have_text("Ready to import")
        expect(page.get_by_role("button", name="Import as comment")).to_be_enabled()

        # The target changes after the server read it but before its answer
        # lands: that answer is out of date, so Import is never offered.
        _choose(page, "two.okf-feedback.json", _note(linked_bundle, "sub/two.md", note_id="5" * 32))
        expect(_heading(page)).to_have_text("Checking the note…")
        route = _parked(page, held, 2)
        response = route.fetch()
        assert response.status == 200 and response.json()["status"] == "ready"
        two = linked_bundle / "sub" / "two.md"
        two.write_bytes(two.read_bytes() + b"\nA later paragraph.\n")
        page.wait_for_function("() => window.__fbiSeen.includes('sub/two')", timeout=15000)
        expect(_heading(page)).to_have_text("Checking the note…")
        route.fulfill(response=response)
        expect(_heading(page)).to_have_text("Preview out of date")
        expect(_heading(page)).to_be_focused()
        expect(result.locator("[data-fbi-message='stale']")).to_contain_text("The target page was updated.")
        expect(page.get_by_role("button", name="Import as comment")).to_have_count(0)

        page.unroute("**" + IMPORT + "/preview")
        page.get_by_role("button", name="Preview again").click()
        expect(result.locator("[data-fbi-message='revision_mismatch']")).to_be_visible()
        assert _rows(page, base) == []
    assert page.failed_documents == []


def test_preview_expires_after_its_lifetime(linked_bundle, page):
    # --no-watch-ui: no SSE stream, so the fast-forward cannot trip the live
    # heartbeat watchdog and stale the preview for a different reason.
    with live_server(linked_bundle, "--no-watch-ui") as base:
        page.clock.install()
        _import_page(page, base)
        _choose(page, "one.okf-feedback.json", _note(linked_bundle, "sub/one.md"))
        expect(_heading(page)).to_have_text("Ready to import")
        page.clock.fast_forward("09:00")
        expect(_heading(page)).to_have_text("Ready to import")
        page.clock.fast_forward("01:00")
        expect(_heading(page)).to_have_text("Preview out of date")
        expect(page.locator("[data-fbi-message='stale']")).to_contain_text("Previews last 10 minutes.")
        expect(page.get_by_role("button", name="Import as comment")).to_have_count(0)
        page.get_by_role("button", name="Preview again").click()
        expect(_heading(page)).to_have_text("Ready to import")


def test_hostile_note_text_stays_inert_on_the_page_and_the_card(linked_bundle, page):
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    body = '<img src=x onerror="alert(1)"> **bold**'
    author = "<b>Mallory</b>"
    quote = "<script>alert(2)</script>"
    with live_server(linked_bundle) as base:
        _import_page(page, base)
        _choose(page, "hostile.okf-feedback.json", _note(
            linked_bundle, "sub/one.md", note_id="4" * 32, body=body, claimed_author=author, quote=quote))
        expect(_heading(page)).to_have_text("Ready to import")
        result = page.locator("#okf-fbi-result")
        expect(result.locator(".okf-fbi__body")).to_have_text(body)
        expect(result.locator(".okf-fbi__facts")).to_contain_text(author + " unverified")
        expect(result.locator("blockquote")).to_have_text(quote)
        assert result.locator("img, b, script").count() == 0
        page.get_by_role("button", name="Import as comment").click()
        expect(_heading(page)).to_have_text("Imported")
        page.get_by_role("button", name="Show the comment").click()
        [row] = _rows(page, base)
        card = page.locator(f"#comment-{row['id']}")
        expect(card.locator(".okf-comment__import-claims")).to_have_text("Note author (unverified): " + author)
        expect(card.locator(".okf-comment__import-quote")).to_have_text(quote)
        assert card.locator(".okf-comment__import img, .okf-comment__import b, .okf-comment__import script").count() == 0
        assert card.locator("img[src='x']").count() == 0
    assert dialogs == []


def test_import_is_local_edit_only(linked_bundle, comment_only_base, page):
    # Public ingress: no page, no Studio entry, no palette command.
    response = page.request.get(comment_only_base + IMPORT)
    assert response.status == 403
    _studio_ready(page, comment_only_base + "/sub/one")
    page.locator(".okf-studio-launcher").click()
    expect(page.locator("#okf-panel")).to_be_visible()
    expect(page.get_by_role("link", name="Import feedback note")).to_have_count(0)
    page.keyboard.press("Escape")
    page.evaluate("() => window.okfLoomStudio.openPalette()")
    expect(page.locator(".okf-palette__item", has_text="Import feedback note")).to_have_count(0)


def test_read_only_studio_explains_and_offers_no_controls(linked_bundle, page):
    with live_server(linked_bundle, "--no-edit") as base:
        page.goto(base + IMPORT, wait_until="load")
        expect(page.get_by_role("heading", name="Import unavailable")).to_be_visible()
        expect(page.locator(".okf-fbi__notice--error")).to_contain_text("This Studio is read-only")
        expect(page.locator("#okf-fbi-file")).to_have_count(0)
        _studio_ready(page, base + "/sub/one")
        page.locator(".okf-studio-launcher").click()
        expect(page.locator("#okf-panel")).to_be_visible()
        expect(page.get_by_role("link", name="Import feedback note")).to_have_count(0)


def test_phone_layout_keeps_the_preview_readable_and_targets_large(linked_bundle, page):
    page.set_viewport_size({"width": 390, "height": 844})
    with live_server(linked_bundle) as base:
        _import_page(page, base)
        long_name = "N" * 80
        _choose(page, "one.okf-feedback.json", _note(
            linked_bundle, "sub/one.md", body="word " * 400, claimed_author=long_name, quote="q" * 300))
        expect(_heading(page)).to_have_text("Ready to import")
        layout = page.evaluate("""() => {
          const r = document.getElementById('okf-fbi-result');
          const buttons = [...r.querySelectorAll('.okf-btn')].filter(b => b.getClientRects().length);
          return {page: document.documentElement.scrollWidth, overflow: r.scrollWidth - r.clientWidth,
                  right: r.getBoundingClientRect().right,
                  short: buttons.filter(b => b.getBoundingClientRect().height < 44).length,
                  buttons: buttons.length};
        }""")
        assert layout["page"] <= 390 and layout["overflow"] <= 0 and layout["right"] <= 390, layout
        assert layout["buttons"] >= 2 and layout["short"] == 0, layout
        # Keyboard order: file input, then the result's actions.
        page.locator("#okf-fbi-file").focus()
        page.keyboard.press("Tab")
        expect(page.get_by_role("button", name="Import as comment")).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.get_by_role("button", name="Choose another file")).to_be_focused()
