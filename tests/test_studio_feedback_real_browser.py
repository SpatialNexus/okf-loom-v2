"""Real browser proof for Studio feedback, navigation and layout.

Every test drives shipped output: a real ``serve`` process, or a real export
(static served over plain HTTP, single-file opened from disk). No product
response is mocked or fulfilled.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from browser_support import expect, free_port, launch_chromium, live_server, managed_server, sync_playwright
from scripts.okf_loom import portable_feedback
from scripts.okf_loom.model import Bundle
from scripts.okf_loom.render import build_site
from scripts.okf_loom.review_links import review_url

pytestmark = pytest.mark.browser

ONE = """---
type: Note
title: One
---

# One

[sibling-link](two.md#details) and [dot-link](./two.md) and
[parent-link](../top.md#top-section) and [dir-index-link](index.md) and
[dir-log-link](log.md#history) and [root-index-link](../index.md) and
[missing-link](../gone/missing.md#nowhere) and [external-link](https://example.com/a.md).
"""


@pytest.fixture
def linked_bundle(tmp_path):
    root = tmp_path / "bundle"
    (root / "sub").mkdir(parents=True)
    (root / "index.md").write_text("# Home\n\nRoot overview.\n", encoding="utf-8")
    (root / "log.md").write_text("# Root log\n", encoding="utf-8")
    (root / "top.md").write_text("---\ntype: Note\ntitle: Top\n---\n\n# Top\n\n## Top section\n\nText.\n", encoding="utf-8")
    (root / "sub" / "index.md").write_text("# Directory\n", encoding="utf-8")
    (root / "sub" / "log.md").write_text(
        "# Sub log\n\n## History\n\nSee [home-from-log](../index.md) and [one-from-log](one.md).\n", encoding="utf-8")
    (root / "sub" / "one.md").write_text(ONE, encoding="utf-8")
    (root / "sub" / "two.md").write_text(
        "---\ntype: Note\ntitle: Two\n---\n\n# Two\n\nIntro.\n\n## Details\n\nMore.\n", encoding="utf-8")
    return root


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = context.new_page()
        pg.failed_documents = []
        pg.on("response", lambda r: pg.failed_documents.append((r.status, r.url))
              if r.request.resource_type == "document" and r.status >= 400 else None)
        yield pg
        context.close()
        browser.close()


def _select(page, url, concept="sub/one"):
    page.goto(url, wait_until="load")
    page.wait_for_selector(f'#detail-body[data-concept-id="{concept}"]', state="visible")


def _link(page, text, scope="#detail-body"):
    return page.locator(f"{scope} a", has_text=text)


def _focused_id(page):
    return page.evaluate("() => document.activeElement && document.activeElement.id")


def _assert_link_honesty(page):
    missing = _link(page, "missing-link")
    assert missing.get_attribute("data-okf-broken") == "1"
    assert missing.get_attribute("href") is None
    assert missing.get_attribute("target") is None
    assert "gone/missing.md" in missing.get_attribute("title")
    external = _link(page, "external-link")
    assert external.get_attribute("target") == "_blank"
    assert external.get_attribute("href") == "https://example.com/a.md"


def test_live_graph_detail_links_reach_real_destinations(linked_bundle, page):
    with live_server(linked_bundle) as base:
        graph = f"{base}/__graph?selected=sub/one"
        _select(page, graph)
        _assert_link_honesty(page)

        _link(page, "sibling-link").click()
        page.wait_for_selector('#detail-body[data-concept-id="sub/two"]')
        assert _focused_id(page) == "details"

        _select(page, graph)
        _link(page, "dot-link").click()
        page.wait_for_selector('#detail-body[data-concept-id="sub/two"]')
        assert _focused_id(page) == "detail-title"

        _select(page, graph)
        _link(page, "parent-link").click()
        page.wait_for_selector('#detail-body[data-concept-id="top"]')
        assert _focused_id(page) == "top-section"

        _select(page, graph)
        _link(page, "dir-log-link").click()
        page.wait_for_url(f"{base}/sub/log.md#history")
        assert page.locator('article[data-okf-document="sub/log.md"]').is_visible()
        # The log's own links work from the log's directory.
        _link(page, "one-from-log", "#okf-main").click()
        page.wait_for_url(f"{base}/sub/one")

        _select(page, graph)
        _link(page, "dir-index-link").click()
        page.wait_for_url(f"{base}/sub/")
        assert page.locator("main.okf-index").is_visible()

        _select(page, graph)
        _link(page, "root-index-link").click()
        page.wait_for_url(f"{base}/")
        assert page.locator("main.okf-index").is_visible()
        assert page.failed_documents == []


@pytest.mark.parametrize("width", [1280, 390])
def test_a_concept_page_fragment_link_opens_in_place_and_history_returns(linked_bundle, page, width):
    """A plain click on a rendered `.md#fragment` link is ordinary same-tab
    navigation: the destination document, its hash, the section in view, and
    Back / Forward restoring both. The documents are long, so the section is
    below the fold until the fragment is honoured."""
    filler = "\n\n".join(f"Filler paragraph {i}." for i in range(60))
    for rel, heading in (("sub/two.md", "## Details"), ("top.md", "## Top section"), ("sub/log.md", "## History")):
        path = linked_bundle / rel
        path.write_text(path.read_text(encoding="utf-8").replace(heading, filler + "\n\n" + heading), encoding="utf-8")
    page.set_viewport_size({"width": width, "height": 900})
    popups = []
    page.context.on("page", lambda opened: popups.append(opened))
    landed = """(id) => { const t = document.getElementById(id); if (!t || location.hash !== '#' + id || !t.matches(':target')) return false;
      const r = t.getBoundingClientRect(); return scrollY > 0 && r.top >= 0 && r.bottom <= innerHeight; }"""
    with live_server(linked_bundle) as base:
        for text, destination, fragment, document in (
            ("sibling-link", "/sub/two", "details", "#okf-main"),
            ("parent-link", "/top", "top-section", "#okf-main"),
            ("dir-log-link", "/sub/log.md", "history", 'article[data-okf-document="sub/log.md"]'),
        ):
            _studio_ready(page, f"{base}/sub/one")
            link = _link(page, text, "#okf-main")
            assert link.get_attribute("target") is None and link.get_attribute("href") == f"{destination}#{fragment}"
            link.click()
            page.wait_for_url(f"{base}{destination}#{fragment}")
            page.wait_for_function(landed, arg=fragment)
            assert page.locator(document).first.is_visible()
            page.go_back()
            page.wait_for_url(f"{base}/sub/one")
            page.wait_for_function("() => !location.hash && !document.querySelector(':target')")
            assert _link(page, text, "#okf-main").is_visible()
            page.go_forward()
            page.wait_for_url(f"{base}{destination}#{fragment}")
            page.wait_for_function(landed, arg=fragment)
        assert popups == [] and len(page.context.pages) == 1
        assert page.failed_documents == []


def test_static_graph_detail_links_reach_emitted_pages(linked_bundle, tmp_path, page):
    out = tmp_path / "static"
    build_site(Bundle.load(linked_bundle), out, target="static")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    argv = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(out)]
    with managed_server(argv, base, tmp_path / "static-http.log"):
        graph = f"{base}/__graph.html?selected=sub/one"
        _select(page, graph)
        _assert_link_honesty(page)

        _link(page, "parent-link").click()
        page.wait_for_selector('#detail-body[data-concept-id="top"]')
        assert _focused_id(page) == "top-section"

        _select(page, graph)
        _link(page, "dir-log-link").click()
        page.wait_for_url(f"{base}/sub/log.html#history")
        assert page.locator('article[data-okf-document="sub/log.md"]').is_visible()
        _link(page, "home-from-log", "#okf-main").click()
        page.wait_for_url(f"{base}/index.html")

        _select(page, graph)
        _link(page, "dir-index-link").click()
        page.wait_for_url(f"{base}/sub/index.html")
        assert page.locator("main.okf-index").is_visible()
        assert page.failed_documents == []


def test_single_file_reads_reserved_documents_without_a_fake_selection(linked_bundle, tmp_path, page):
    out = tmp_path / "single"
    build_site(Bundle.load(linked_bundle), out, target="single-file")
    url = (out / "index.html").as_uri() + "?selected=sub/one"
    _select(page, url)
    _assert_link_honesty(page)

    _link(page, "sibling-link").click()
    page.wait_for_selector('#detail-body[data-concept-id="sub/two"]')
    assert _focused_id(page) == "details"

    _select(page, url)
    _link(page, "dir-log-link").click()
    pane = page.locator("#detail-document")
    pane.wait_for(state="visible")
    assert page.locator("#detail-document-title").inner_text() == "sub/log.md"
    assert page.locator('#detail-document-body[data-okf-document="sub/log.md"]').is_visible()
    assert _focused_id(page) == "history"
    assert page.locator("#detail-content").is_hidden()
    assert "selected=" not in page.url  # no fabricated node selection

    # Reserved → reserved keeps the way back to the concept the reader left.
    _link(page, "home-from-log", "#detail-document-body").click()
    page.wait_for_function("() => document.getElementById('detail-document-title').textContent === 'index.md'")
    back = page.get_by_role("button", name="Back to One")
    back.press("Enter")
    page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="visible")
    assert pane.is_hidden()
    assert _focused_id(page) == "detail-title"

    # Reserved → concept selects a real node and dismisses the reader.
    _link(page, "dir-log-link").click()
    pane.wait_for(state="visible")
    _link(page, "one-from-log", "#detail-document-body").click()
    page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="visible")
    assert pane.is_hidden()


# ---- Comment on any page ---------------------------------------------------

SURFACES = [
    # url path, stored owner, composer label
    ("/", {"concept": "", "document": "index.md"}, "index.md"),
    ("/sub/", {"concept": "", "document": "sub/index.md"}, "sub/index.md"),
    ("/log.md", {"concept": "", "document": "log.md"}, "log.md"),
    ("/sub/log.md", {"concept": "", "document": "sub/log.md"}, "sub/log.md"),
    ("/sub/one", {"concept": "sub/one"}, "sub/one"),
]


def _studio_ready(page, url):
    page.goto(url, wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    settled = page.wait_for_function(
        "() => { const s = window.okfLoomStudio.state.commentsStatus;"
        " return ['ready', 'error'].includes(s) ? s : false; }").json_value()
    assert settled == "ready"


def _rows(page, base):
    data = page.request.get(f"{base}/__comments").json()
    return data["comments"] if isinstance(data, dict) else data


def _comment_here(page, body, noun="page"):
    page.get_by_role("button", name=f"Comment on this {noun}", exact=True).click()
    box = page.get_by_role("textbox", name="Comment for agent")
    expect(box).to_be_focused()
    box.fill(body)
    page.get_by_role("button", name="Send", exact=True).click()
    # Saved, not merely sent: the optimistic row is visible while the POST is
    # still in flight, and it must not be mistaken for a stored comment.
    expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")
    _saved_card(page, body)
    expect(box).to_have_value("")


def _saved_card(page, body):
    card = page.locator(".okf-comment", has_text=body).filter(has_not=page.locator(".okf-comment__posting")).first
    expect(card).to_be_visible()
    return card


def _reply(page, parent_body, body):
    _saved_card(page, parent_body).get_by_role("button", name="Reply", exact=True).first.click()
    box = page.get_by_role("textbox", name="Reply to comment")
    box.fill(body)
    box.press("Control+Enter")
    saved = page.locator(".okf-comment--reply", has_text=body).filter(has_not=page.locator(".okf-comment__posting")).first
    expect(saved).to_be_visible()
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_count(0)


def test_every_document_surface_takes_a_page_comment_with_its_own_identity(linked_bundle, page):
    with live_server(linked_bundle) as base:
        for path, owner, label in SURFACES:
            _studio_ready(page, base + path)
            group = page.get_by_role("group", name="Page feedback")
            expect(group.get_by_role("button", name="Comment on this page", exact=True)).to_be_in_viewport()
            body = f"Feedback for {label}"
            page.get_by_role("button", name="Comment on this page", exact=True).click()
            expect(page.locator(".okf-composer__anchor")).to_contain_text("On page:")
            expect(page.locator(".okf-composer__anchor code")).to_have_text(label)
            page.keyboard.press("Escape")
            _comment_here(page, body)
            expect(group.get_by_role("button", name="1 open comment on this page")).to_be_visible()
            row = next(r for r in _rows(page, base) if r["body"] == body)
            # Reserved documents take the closed document-wide anchor; a concept
            # keeps its established page-level anchor. Neither carries offsets.
            assert row["anchor"] == ({"kind": "page"} if "document" in owner else {"kind": "concept"})
            assert row["concept"] == owner["concept"]
            if "document" in owner:
                assert row["document"] == owner["document"]

        # Reply keeps its parent's target; both survive a canonical reload.
        _studio_ready(page, base + "/sub/log.md")
        page.get_by_role("button", name="1 open comment on this page").click()
        _reply(page, "Feedback for sub/log.md", "Reply on the log")
        stored = next(r for r in _rows(page, base) if r["body"] == "Reply on the log")
        assert (stored["concept"], stored["document"]) == ("", "sub/log.md")

        _studio_ready(page, base + "/sub/log.md")
        page.get_by_role("group", name="Page feedback").get_by_role(
            "button", name="2 open comments on this page", exact=True).click()
        roots = page.locator(".okf-comment--level-0")
        # Scoped to this page by default, with an honest way to the rest.
        expect(roots).to_have_count(1)
        expect(page.locator(".okf-comment--reply", has_text="Reply on the log")).to_be_visible()
        expect(page.locator(".okf-comment-list__elsewhere")).to_contain_text("4 threads are on other pages")
        page.get_by_role("button", name="Show all pages", exact=True).click()
        expect(roots).to_have_count(len(SURFACES))
        expect(page.get_by_label("Show comments from")).to_be_focused()
        expect(page.locator(".okf-comment__target", has_text="On this page")).to_have_count(1)
        page.locator(".okf-comment__target-link", has_text="sub/index.md").click()
        page.wait_for_url(base + "/sub/")
        assert page.failed_documents == []


def test_graph_concept_feedback_follows_the_selected_node(linked_bundle, page):
    with live_server(linked_bundle) as base:
        _studio_ready(page, f"{base}/__graph?selected=sub/one")
        page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="visible")
        _comment_here(page, "From the graph about one", noun="concept")
        group = page.get_by_role("group", name="Concept feedback")
        expect(group.get_by_role("button", name="1 open comment on this concept")).to_be_visible()
        row = next(r for r in _rows(page, base) if r["body"] == "From the graph about one")
        assert row["concept"] == "sub/one" and row["anchor"] == {"kind": "concept"}

        # A link in the preview selects another concept with no navigation: the
        # entry, the count and the scoped list all follow the pane's identity.
        _link(page, "sibling-link").click()
        page.wait_for_selector('#detail-body[data-concept-id="sub/two"]', state="visible")
        expect(group.get_by_role("button", name="Comment on this concept", exact=True)).to_be_visible()
        expect(group.locator(".okf-page-feedback__count")).to_be_hidden()
        expect(page.locator(".okf-comment--level-0")).to_have_count(0)
        expect(page.locator(".okf-comment-list__elsewhere")).to_contain_text("1 thread is on other pages")


def _public_ingress_base(linked_bundle, *, allow_public_comment: bool):
    """The production handler behind its dedicated public-ingress listener.

    Public authority is listener identity, not a flag or a header, so the
    honest way to reach it is that listener itself — bound to loopback here,
    with no tunnel process and no public endpoint.
    """
    import threading
    from http.server import ThreadingHTTPServer

    from scripts.okf_loom.server import OKFWikiHandler, _start_public_ingress, _stop_public_ingress
    from scripts.okf_loom.studio import Studio

    studio = Studio.for_bundle(linked_bundle)
    studio.ensure_session()
    server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.bundle = Bundle.load(linked_bundle)
    server.studio = studio
    server.config = {}
    server.name = "Feedback"
    server.plugin = None
    server.csrf_token = "local-secret"
    server.allowed_hosts = ("127.0.0.1",)
    server.studio_edit = True
    server.studio_live = True
    server._state_lock = threading.RLock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ingress = _start_public_ingress(server, allow_public_edit=False,
                                    allow_public_comment=allow_public_comment)
    base = f"http://127.0.0.1:{ingress.server_port}"
    server.tunnel_url = base
    try:
        yield base
    finally:
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def comment_only_base(linked_bundle):
    yield from _public_ingress_base(linked_bundle, allow_public_comment=True)


def test_comment_only_session_comments_and_replies_but_holds_no_edit_power(comment_only_base, page):
    base = comment_only_base
    _studio_ready(page, base + "/sub/one")
    boot = page.evaluate("() => JSON.parse(document.getElementById('okf-studio-bootstrap').textContent)")
    assert (boot["edit"], boot["comment"], boot["authority_mode"]) == (False, True, "public-comment")
    expect(page.get_by_role("button", name="Studio \u00b7 Comment-only")).to_be_visible()

    _comment_here(page, "Comment-only feedback")
    card = page.locator(".okf-comment", has_text="Comment-only feedback").first
    assert [t.strip() for t in card.locator(".okf-comment__actions button").all_inner_texts()] == ["Reply"]
    _reply(page, "Comment-only feedback", "Comment-only reply")

    # No edit or lifecycle affordance is offered anywhere in the session...
    for name in ("Edit", "Save", "Resolve", "Dismiss", "Archive", "Re-anchor", "Undo"):
        expect(page.get_by_role("button", name=name, exact=True)).to_have_count(0)
    # ...and the same session is refused by the server if it tries anyway.
    refused = page.evaluate("""async () => {
      const token = JSON.parse(document.getElementById('okf-studio-bootstrap').textContent).token || '';
      const out = {};
      for (const route of ['/__apply', '/__undo', '/__comment-update', '/__comment-reanchor', '/__tunnel']) {
        const r = await fetch(route, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-OKF-Token': token }, body: '{}' });
        out[route] = r.status;
      }
      return out;
    }""")
    assert set(refused.values()) == {403}, refused

    # Reserved pages take comments in the same session.
    _studio_ready(page, base + "/log.md")
    _comment_here(page, "Comment-only on the log")
    assert page.failed_documents == []


# ---- F2: undo capability gating over seeded history -------------------------


def _install_undo_journal(page) -> None:
    """Observe real /__undo requests (url, body, status) in the page."""
    installed = page.evaluate(
        """
        () => {
            if (window.__okfUndoJournal) return true;
            window.__okfUndoJournal = [];
            const raw = window.fetch.bind(window);
            window.fetch = function (...args) {
                const url = String(args[0] || "");
                const isUndo = url.indexOf("/__undo") >= 0;
                const rec = { url, body: null };
                if (isUndo) {
                    rec.body = args[1] && typeof args[1].body === "string" ? args[1].body : null;
                    window.__okfUndoJournal.push(rec);
                }
                return raw.apply(window, args).then(
                    (res) => { if (isUndo) rec.status = res.status; return res; },
                    (err) => { if (isUndo) rec.error = String(err); throw err; });
            };
            return true;
        }
        """
    )
    assert installed is True


def _undo_requests(page) -> list:
    return page.evaluate("() => window.__okfUndoJournal")


def _seed_undoable_history(bundle) -> None:
    """Seed one real attributed, undoable mutator write via the CLI.

    With a live session the mutator routes through the studio funnel, so the
    write carries an undo snapshot, an ``undoable`` activity event, and a
    content-hash ``detail.before`` pointer — the exact history shape whose
    gating this section proves.
    """
    _okf(bundle, "update-section", "--bundle", str(bundle), "--id", "sub/two",
         "--heading", "Details",
         "--body", "SEEDED undoable history replacement for the undo-gating proof.")


def _assert_changes_history_readable_without_operative_undo(page) -> None:
    """Open Changes through the real UI and assert the honest gated row.

    History stays readable; the Undo control renders disabled with the
    reason in its accessible name, and interacting with it can issue no
    request at all.
    """
    page.locator(".okf-studio-launcher").click()
    page.get_by_role("button", name="Changes").click()
    page.wait_for_selector(".okf-panel__body .okf-change", timeout=8000)
    row = page.locator(".okf-change", has_text="sub/two").first
    expect(row).to_be_visible()
    undo = page.locator(".okf-change__undo").first
    assert undo.is_disabled(), "Undo must not be operative without edit authority"
    aria = undo.get_attribute("aria-label") or ""
    assert "read-only" in aria, aria
    assert "read-only" in (undo.get_attribute("title") or "")
    # The one honest interaction — attempting the control — cannot fire.
    undo.evaluate("el => el.click()")
    assert _undo_requests(page) == []


def _assert_gated_toast_explains_instead_of_undo(page) -> None:
    """An undoable activity toast in a no-edit session explains, never acts."""
    page.evaluate(
        """
        () => window.okfLoomLive.emit('activity', {
            id: 'undo-gate-toast-1', actor: 'agent', action: 'update_section',
            ids: ['sub/two'], summary: 'update_section on sub/two',
            undoable: true, ts: new Date().toISOString(),
        })
        """
    )
    page.wait_for_selector(".okf-toast", timeout=8000)
    toast = page.locator(".okf-toast").last
    expect(toast.locator(".okf-toast__action")).to_have_count(1)
    expect(toast.get_by_role("button", name="View")).to_be_visible()
    expect(toast.locator(".okf-toast__action", has_text=re.compile("Undo"))).to_have_count(0)
    expect(toast).to_contain_text("Undo is unavailable in read-only views")


@pytest.fixture(params=[("public-comment", True), ("public-read", False)])
def public_ingress(linked_bundle, request):
    """`(base, authority_mode, allows_comment)` behind the real public ingress.

    Declared as a direct fixture (not resolved mid-test) so it is torn down
    AFTER the browser page fixture: closing the page drops the page's SSE
    stream before the ingress listener is stopped, which the listener's
    close sequencing requires.
    """
    expected_mode, allow_comment = request.param
    flow = _public_ingress_base(linked_bundle, allow_public_comment=allow_comment)
    base = flow.send(None)
    try:
        yield base, expected_mode, allow_comment
    finally:
        flow.close()


def test_public_sessions_keep_seeded_history_readable_without_operative_undo(
    public_ingress, linked_bundle, page,
) -> None:
    """F2: public-ingress sessions see seeded undoable history read-only.

    Bootstrap reports edit:false for the mode; the Changes row renders its
    Undo control disabled with an explanation; the toast offers View with
    the same explanation; and normal interaction issues no /__undo request —
    the server's own denial stays the authority (proved by the retained 403
    in the comment-only denial test).
    """
    base, expected_mode, expected_comment = public_ingress
    _seed_undoable_history(linked_bundle)
    _studio_ready(page, base + "/sub/two")
    _install_undo_journal(page)
    boot = page.evaluate(
        "() => JSON.parse(document.getElementById('okf-studio-bootstrap').textContent)")
    assert (boot["edit"], boot["comment"], boot["authority_mode"]) == (
        False, expected_comment, expected_mode), boot

    _assert_changes_history_readable_without_operative_undo(page)
    _assert_gated_toast_explains_instead_of_undo(page)
    assert _undo_requests(page) == []


def test_local_read_session_keeps_seeded_history_readable_without_operative_undo(
    linked_bundle, page,
) -> None:
    """F2: a real ``serve --no-edit`` session (local-read) with history seeded
    while it runs — the watcher delivers the live activity, the toast must
    explain instead of offering Undo, the Changes row must render the
    disabled explained control, and the server's 403 denial still stands.
    """
    two = linked_bundle / "sub" / "two.md"
    with live_server(linked_bundle, "--no-edit") as base:
        _studio_ready(page, base + "/sub/two")
        _install_undo_journal(page)
        _seed_undoable_history(linked_bundle)
        assert "SEEDED undoable history replacement" in two.read_text(encoding="utf-8")

        # The real watcher re-publishes the CLI write; the live toast gates.
        page.wait_for_selector(".okf-toast", timeout=15000)
        toast = page.locator(".okf-toast").last
        expect(toast.locator(".okf-toast__action", has_text=re.compile("Undo"))).to_have_count(0)
        expect(toast).to_contain_text("Undo is unavailable in read-only views")
        assert _undo_requests(page) == []

        _assert_changes_history_readable_without_operative_undo(page)

        boot = page.evaluate(
            "() => JSON.parse(document.getElementById('okf-studio-bootstrap').textContent)")
        assert (boot["edit"], boot["authority_mode"]) == (False, "local-read"), boot
        # The UI never asked (proved above); the server's denial remains the
        # authority for anything that does — exactly one request, the probe
        # itself, refused with 403.
        refused = page.evaluate(
            """
            async () => {
                const r = await fetch('/__undo', { method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-OKF-Token': '' },
                    body: JSON.stringify({ concept: 'sub/two', rev: '0'.repeat(12) }) });
                return r.status;
            }
            """
        )
        assert refused == 403, refused
        probe = _undo_requests(page)
        assert len(probe) == 1 and probe[0]["status"] == 403, probe


# ---- Truthful agent availability ---------------------------------------------

def _okf(bundle, *argv):
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([str(root / "scripts" / "okf-loom"), *argv], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, (argv, done.stdout, done.stderr)
    return done.stdout


def test_agent_availability_is_reported_from_evidence_not_from_the_watching_flag(linked_bundle, page):
    with live_server(linked_bundle) as base:
        _studio_ready(page, base + "/sub/one")
        _comment_here(page, "Please link this to Two")
        line = page.locator(".okf-comment-composer-section .okf-agent-availability")
        expect(line).to_have_attribute("data-level", "none")
        expect(line).to_contain_text("Comments are saved and wait until an external agent picks them up")

        # The presence flag alone is named as a flag, never as a running agent.
        _okf(linked_bundle, "presence", str(linked_bundle), "--state", "watching")
        expect(line).to_have_attribute("data-level", "flagged")
        expect(line).to_contain_text("not proof that an agent is running")
        page.locator(".okf-studio-launcher").click()
        toggle = page.locator(".okf-watch-toggle")
        expect(toggle).to_be_visible()
        assert "does not start an agent" in toggle.get_attribute("title")
        described = page.locator("#" + toggle.get_attribute("aria-describedby"))
        expect(described).to_contain_text("It does not start an agent")
        page.locator("#okf-panel").get_by_role("button", name="Comments", exact=False).first.click()
        expect(line).to_have_attribute("data-level", "flagged")

        # A claim is durable evidence: the card and the line both say so.
        row = next(r for r in _rows(page, base) if r["body"] == "Please link this to Two")
        _okf(linked_bundle, "comment-claim", str(linked_bundle), row["id"], "--summary", "adding the link")
        card = page.locator("#comment-" + row["id"])
        expect(card.locator(".okf-comment__state")).to_have_text("claimed by agent")
        expect(line).to_have_attribute("data-level", "active")
        expect(line).to_contain_text("An agent has claimed 1 comment")

        _okf(linked_bundle, "comment-resolve", str(linked_bundle), row["id"], "--summary", "done: link added")
        expect(card.locator(".okf-comment__state")).to_have_text("resolved")
        expect(line).to_have_attribute("data-level", "seen")
        expect(line).to_contain_text("Whether it is still running is not known here")
        expect(page.get_by_role("group", name="Page feedback").get_by_role(
            "button", name="1 resolved comment on this page", exact=True)).to_be_visible()


_CARD_IN_RAIL_JS = """(id) => {
  const rail = document.querySelector('#okf-panel .okf-panel__body').getBoundingClientRect();
  const card = document.getElementById('comment-' + id).getBoundingClientRect();
  return {railTop: rail.top, railBottom: rail.bottom, top: card.top, bottom: card.bottom};
}"""


def test_the_comments_rail_keeps_the_whole_resolved_card_in_view_as_the_agent_replies(linked_bundle, page):
    """A reader parked at the end of the rail watches the last card grow: the
    agent's claim, then its DONE summary and reply. The live refresh used to
    restore the old scroll offset, so the finished card ended cut off below
    the fold of a 720 px tall rail."""
    page.set_viewport_size({"width": 1280, "height": 720})
    with live_server(linked_bundle) as base:
        _studio_ready(page, base + "/sub/one")
        _comment_here(page, "Please link this to Two")
        expect(page.locator("body")).to_have_class(re.compile(r"\bokf-panel-rail\b"))
        row = next(r for r in _rows(page, base) if r["body"] == "Please link this to Two")
        card = page.locator("#comment-" + row["id"])
        rail = page.locator("#okf-panel .okf-panel__body")
        assert rail.evaluate("b => b.scrollHeight - b.clientHeight") > 8, "the rail must scroll"

        # Anywhere above the end, a live refresh keeps the reader's exact place.
        rail.evaluate("b => { b.scrollTop = 4; }")
        _okf(linked_bundle, "comment-claim", str(linked_bundle), row["id"], "--summary", "adding the link")
        expect(card.locator(".okf-comment__state")).to_have_text("claimed by agent")
        assert rail.evaluate("b => b.scrollTop") == 4

        # At the end, the reader stays at the end while the card grows.
        rail.evaluate("b => { b.scrollTop = b.scrollHeight; }")
        _okf(linked_bundle, "comment-resolve", str(linked_bundle), row["id"], "--summary", "done: link added",
             "--reply", "Linked One to Two in the opening sentence.")
        expect(card.locator(".okf-comment__state")).to_have_text("resolved")
        expect(card.locator(".okf-comment__reply")).to_contain_text("Linked One to Two in the opening sentence.")
        assert rail.evaluate("b => b.scrollHeight - b.clientHeight - b.scrollTop") <= 1
        box = page.evaluate(_CARD_IN_RAIL_JS, row["id"])
        assert box["railTop"] <= box["top"] and box["bottom"] <= box["railBottom"], box


_TOAST_BESIDE_RAIL_JS = """(id) => {
  const toast = [...document.querySelectorAll('.okf-toast')].pop().getBoundingClientRect();
  const panel = document.getElementById('okf-panel').getBoundingClientRect();
  const card = document.getElementById('comment-' + id);
  const r = card.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.bottom - 6);
  return {toastLeft: toast.left, toastRight: toast.right, toastBottom: toast.bottom,
          panelLeft: panel.left, width: innerWidth, height: innerHeight,
          cardBottomUncovered: !!hit && card.contains(hit)};
}"""


def test_an_agent_change_toast_lands_beside_the_comments_rail_not_on_its_last_card(linked_bundle, page):
    """The rail is where the reader watches the agent's card. A change toast
    fixed to the viewport corner used to sit on the rail and cover the bottom
    of that last card; it now belongs to the reading column beside the rail."""
    page.set_viewport_size({"width": 1280, "height": 720})
    with live_server(linked_bundle) as base:
        _studio_ready(page, base + "/sub/one")
        _comment_here(page, "Please link this to Two")
        expect(page.locator("body")).to_have_class(re.compile(r"\bokf-panel-rail\b"))
        row = next(r for r in _rows(page, base) if r["body"] == "Please link this to Two")
        card = page.locator("#comment-" + row["id"])
        page.locator("#okf-panel .okf-panel__body").evaluate("b => { b.scrollTop = b.scrollHeight; }")
        _okf(linked_bundle, "comment-resolve", str(linked_bundle), row["id"], "--summary", "done: link added",
             "--reply", "Linked One to Two in the opening sentence.")
        expect(card.locator(".okf-comment__reply")).to_contain_text("Linked One to Two in the opening sentence.")

        _okf(linked_bundle, "replace-text", "--bundle", str(linked_bundle), "--id", "sub/two",
             "--old", "Intro.", "--new", "Intro, linked from One.")
        toast = page.locator(".okf-toast").last
        expect(toast).to_be_visible(timeout=15_000)
        box = page.evaluate(_TOAST_BESIDE_RAIL_JS, row["id"])
        assert box["toastRight"] <= box["panelLeft"] - 8, box
        assert box["toastLeft"] >= 0 and box["toastBottom"] <= box["height"], box
        assert box["cardBottomUncovered"], box


def test_an_agent_text_edit_toast_names_the_page_in_plain_words(linked_bundle, page):
    """A text edit used to toast "Docs-agent made a change to Field_notes":
    the op had no verb and the target was the raw last id segment. The page
    being read is named by its own title; any other concept by a readable
    form of its id."""
    (linked_bundle / "sub" / "field_notes.md").write_text(
        "---\ntype: Note\ntitle: Notes from the field\n---\n\n# Notes from the field\n\nFirst visit.\n",
        encoding="utf-8")
    (linked_bundle / "sub" / "order_lines.md").write_text(
        "---\ntype: Note\ntitle: What was ordered\n---\n\n# What was ordered\n\nOne line.\n", encoding="utf-8")
    with live_server(linked_bundle) as base:
        _studio_ready(page, base + "/sub/field_notes")
        _okf(linked_bundle, "replace-text", "--bundle", str(linked_bundle), "--id", "sub/field_notes",
             "--old", "First visit.", "--new", "First visit, written up.", "--actor", "docs-agent")
        expect(page.locator(".okf-toast").last).to_contain_text(
            "Docs-agent edited text in Notes from the field", timeout=15_000)

        _okf(linked_bundle, "replace-text", "--bundle", str(linked_bundle), "--id", "sub/order_lines",
             "--old", "One line.", "--new", "Two lines.", "--actor", "docs-agent")
        expect(page.locator(".okf-toast", has_text="Order lines")).to_contain_text(
            "Docs-agent edited text in Order lines", timeout=15_000)
        for text in page.locator(".okf-toast").all_inner_texts():
            assert "_" not in text and "made a change" not in text, text


LONG_READ = ("---\ntype: Note\ntitle: Long read\n---\n\n# Long read\n\n"
             + "\n\n".join(f"Paragraph {n} of a long read. " + "Plain words keep the reader here. " * 6
                          for n in range(1, 40)) + "\n")

_READING_PLACE_JS = """() => {
  const p = document.querySelectorAll('.okf-page__body p')[3].getBoundingClientRect();
  const tools = document.querySelector('.okf-document-tools').getBoundingClientRect();
  const chrome = document.querySelector('.okf-concept-viewbar').getBoundingClientRect();
  return {scrollY: scrollY, paragraph: p.top, toolsBottom: tools.bottom, toolsHeight: tools.height,
          chromeBottom: chrome.bottom};
}"""


def _first_comment_from_the_rail(page, base, path, body):
    """Open the Comments rail on ``path``; return a function that posts ``body``."""
    _studio_ready(page, base + path)
    if not page.evaluate("() => document.body.classList.contains('okf-panel-rail')"):
        page.locator("[data-okf-live-utilities] button", has_text=re.compile(r"^\s*Comments\s*$")).click()
    expect(page.locator("body")).to_have_class(re.compile(r"\bokf-panel-rail\b"))
    count = page.get_by_role("group", name="Page feedback").get_by_role(
        "button", name="1 open comment on this page", exact=True)
    expect(count).to_be_hidden()

    def post():
        page.get_by_role("textbox", name="Comment for agent").fill(body)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")
        expect(count).to_be_visible()
    return post


_TOOLS_UNDER_CHROME_JS = """(gap) => {
  const tools = document.querySelector('.okf-document-tools').getBoundingClientRect();
  const chrome = document.querySelector('.okf-concept-viewbar').getBoundingClientRect();
  window.scrollBy({top: tools.bottom - (chrome.bottom - gap), behavior: 'instant'});
}"""


def test_the_first_comment_on_a_page_does_not_move_the_text_being_read(linked_bundle, page):
    """The first comment on a page adds an "open comment" count to the
    document tools above the body. With the bottom of that strip scrolled up
    behind the sticky chrome (still inside the viewport, so the browser's own
    scroll anchor lands in the strip), the paragraph the reader was on used
    to jump down a row. A strip scrolled clean out of the viewport must not
    be compensated twice, and a strip the reader can see grows in place."""
    page.set_viewport_size({"width": 1280, "height": 720})
    for name in ("long_under", "long_above", "long_top"):
        (linked_bundle / "sub" / f"{name}.md").write_text(LONG_READ, encoding="utf-8")
    with live_server(linked_bundle) as base:
        for path in ("/sub/long_under", "/sub/long_above"):
            post = _first_comment_from_the_rail(page, base, path, "Is this long read too long?")
            if path.endswith("under"):
                page.evaluate(_TOOLS_UNDER_CHROME_JS, 8)
            else:
                page.evaluate("() => { const p = document.querySelectorAll('.okf-page__body p')[3];"
                              " window.scrollBy({top: p.getBoundingClientRect().top - 240, behavior: 'instant'}); }")
            before = page.evaluate(_READING_PLACE_JS)
            if path.endswith("under"):
                assert 0 < before["toolsBottom"] <= before["chromeBottom"], before
            else:
                assert before["toolsBottom"] <= 0, before
            post()
            after = page.evaluate(_READING_PLACE_JS)
            assert after["toolsHeight"] > before["toolsHeight"], (path, before, after)
            assert abs(after["paragraph"] - before["paragraph"]) <= 1, (path, before, after)

        post = _first_comment_from_the_rail(page, base, "/sub/long_top", "Too long?")
        before = page.evaluate(_READING_PLACE_JS)
        assert before["scrollY"] == 0 and before["toolsBottom"] > before["chromeBottom"], before
        post()
        after = page.evaluate(_READING_PLACE_JS)
        assert after["toolsHeight"] > before["toolsHeight"], (before, after)
        assert after["scrollY"] == 0, (before, after)


def _events(page, base):
    data = page.request.get(f"{base}/__data/events?limit=500").json()
    return data["events"] if isinstance(data, dict) else data


def test_a_named_agents_last_action_is_dated_by_its_own_action_not_by_a_later_archive(linked_bundle, page):
    """Real CLI claim/resolve under a named label, then a person archives the thread."""
    label, name = "aic/Polaris", "Agent \u201caic/Polaris\u201d"
    with live_server(linked_bundle) as base:
        _studio_ready(page, base + "/sub/one")
        _comment_here(page, "Please link this to Two")
        line = page.locator(".okf-comment-composer-section .okf-agent-availability")
        expect(line).to_have_attribute("data-level", "none")
        row = next(r for r in _rows(page, base) if r["body"] == "Please link this to Two")

        _okf(linked_bundle, "comment-claim", str(linked_bundle), row["id"], "--actor", label, "--summary", "adding the link")
        card = page.locator("#comment-" + row["id"])
        expect(card.locator(".okf-comment__state")).to_have_text("claimed by " + label)
        expect(line).to_have_attribute("data-level", "active")
        expect(line).to_have_text(name + " has claimed 1 comment in this session.")

        _okf(linked_bundle, "comment-resolve", str(linked_bundle), row["id"], "--actor", label, "--summary", "done: link added")
        expect(card.locator(".okf-comment__state")).to_have_text("resolved")
        resolved = next(r for r in _rows(page, base) if r["id"] == row["id"])
        acted = [e for e in _events(page, base) if e.get("operation") == "lifecycle" and e.get("actor") == label]
        assert max(e["ts"] for e in acted) == resolved["updated_at"] and "claimed_at" not in resolved
        shown = page.evaluate("ts => new Date(ts).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit', second: '2-digit'})",
                              resolved["updated_at"])
        said = name + " last acted in this session " + shown + ". Whether it is still running is not known here."
        expect(line).to_have_text(said)

        # A person archives in a LATER clock second, so a borrowed updated_at
        # would visibly change the sentence. (A real precondition, not a pause.)
        page.wait_for_function("ts => Math.floor(Date.now() / 1000) > Math.floor(Date.parse(ts) / 1000)",
                               arg=resolved["updated_at"], timeout=5000)
        card.get_by_role("button", name="Archive thread", exact=True).click()
        page.wait_for_function("id => window.okfLoomStudio.state.comments.some(c => c.id === id && c.archived)", arg=row["id"])
        archived = next(r for r in _rows(page, base) if r["id"] == row["id"])
        assert archived["claimed_by"] == label and archived["updated_at"] > resolved["updated_at"]
        latest = max((e for e in _events(page, base) if e.get("operation") == "lifecycle"), key=lambda e: e["ts"])
        assert (latest["actor"], latest["ts"]) == ("user", archived["updated_at"])
        expect(line).to_have_text(said)

        # Durable: a fresh load reads the same evidence from the feed and rows.
        _studio_ready(page, base + "/sub/one")
        page.get_by_role("button", name="Comment on this page", exact=True).click()
        expect(line).to_have_text(said)
        # The activity panel lists the named agent's actions, not the person's.
        page.get_by_role("button", name="Close panel").click()
        page.locator(".okf-studio-launcher").click()
        page.locator("#okf-panel").get_by_role("button", name="Agent activity", exact=False).first.click()
        expect(page.locator("#okf-panel .okf-agent-availability")).to_have_text(said)
        expect(page.locator("#okf-panel .okf-empty-state")).to_have_count(0)


def test_agent_availability_never_borrows_a_date_or_an_unattributed_event(linked_bundle, page):
    """Evidence rules over injected rows/events: legacy shapes no server writes today."""
    with live_server(linked_bundle) as base:
        _studio_ready(page, base + "/sub/one")
        page.wait_for_function("() => window.okfLoomStudio.state.eventsReady")
        verdict = """([comments, events, presence]) => { const s = window.okfLoomStudio.state;
            s.comments = comments; s.events = events; s.presence = presence || {state: 'idle'};
            const v = window.okfLoomStudio._agentAvailability(); return [v.level, v.text]; }"""
        held = {"id": "c1", "state": "resolved", "actor": "user", "ts": "2026-01-01T10:00:00Z",
                "claimed_by": "aic/Polaris", "updated_at": "2026-01-03T12:00:00Z", "archived": True}
        # A retained owner proves a past claim; updated_at is the archive's time.
        level, text = page.evaluate(verdict, [[held], [], None])
        assert level == "seen" and "the time it last acted is not recorded" in text and "aic/Polaris" in text
        assert "12:00" not in text and "10:00" not in text
        # An old unmarked comment event repeats the author and creation time, and
        # a person's attributed action is not an agent's. Neither dates anything.
        unmarked = {"id": "e1", "type": "comment", "actor": "user", "ts": "2026-01-01T10:00:00Z", "claimed_by": "aic/Polaris", "state": "claimed"}
        person = {"id": "e2", "type": "comment", "operation": "lifecycle", "actor": "user", "ts": "2026-01-03T12:00:00Z"}
        unknown = {"id": "e3", "type": "comment", "operation": "lifecycle", "actor": None, "ts": "2026-01-03T12:00:01Z"}
        watcher = {"id": "e4", "type": "activity", "actor": "cli", "ts": "2026-01-03T12:00:02Z"}
        assert page.evaluate(verdict, [[held], [unmarked, person, unknown, watcher], None])[1] == text
        # An attributed named-agent action, an agent-authored reply and a current
        # valid claim each date themselves; a malformed claim time dates nothing.
        attributed = {"id": "e5", "type": "comment", "operation": "lifecycle", "actor": "aic/Polaris", "ts": "2026-01-02T09:30:00Z"}
        level, text = page.evaluate(verdict, [[held], [unmarked, person, attributed], None])
        assert level == "seen" and "\u201caic/Polaris\u201d last acted" in text and "12:00" not in text
        reply = {"id": "c2", "parent_id": "c1", "state": "open", "actor": "agent", "ts": "2026-01-02T11:45:00Z", "body": "which page?"}
        level, text = page.evaluate(verdict, [[held, reply], [], None])
        assert level == "seen" and text.startswith("An agent last acted")
        claim = {"id": "c3", "state": "claimed", "actor": "user", "ts": "2026-01-01T10:00:00Z", "claimed_by": "agent", "claimed_at": "not-a-time"}
        level, text = page.evaluate(verdict, [[claim], [], None])
        assert (level, text) == ("active", "An agent has claimed 1 comment in this session.")
        # Presence alone is a flag; nobody's comment makes an agent available.
        mine = {"id": "c4", "state": "open", "actor": "user", "ts": "2026-01-01T10:00:00Z", "updated_at": "2026-01-05T10:00:00Z"}
        level, text = page.evaluate(verdict, [[mine], [person], {"state": "watching"}])
        assert level == "flagged" and "not proof that an agent is running" in text
        assert page.evaluate(verdict, [[mine], [person], None])[0] == "none"


def _append_sharing_event(bundle, action, summary, *, legacy_actor=None):
    """Append the event the server writes when a public tunnel attaches,
    detaches or is lost, through the server's own writer
    (server.py ``_emit_owned_tunnel_activity``). ``legacy_actor`` instead
    appends the same row with an older label, as sessions recorded before
    tunnel rows were attributed to ``cli``. No tunnel runs: the page only
    reads the session feed, and a live server re-broadcasts appends made by
    another process."""
    from scripts.okf_loom.server import _emit_owned_tunnel_activity
    from scripts.okf_loom.studio import Studio

    studio = Studio.for_bundle(bundle)
    if legacy_actor is None:
        _emit_owned_tunnel_activity(studio, summary, action=action)
        return
    studio.append_event({
        "type": "activity", "actor": legacy_actor, "origin": "tunnel", "action": action,
        "ids": [], "summary": summary, "undoable": False,
    })


def test_sharing_the_studio_is_listed_as_agent_activity_not_as_a_bundle_change(linked_bundle, page):
    """R3: a tunnel attach or detach writes nothing to the bundle. Changes, its
    unread count and the recent-writes count leave it out; Agent activity lists
    it under Sharing, and a toast about sharing alone opens Agent activity."""
    with live_server(linked_bundle) as base:
        _studio_ready(page, base + "/sub/one")
        page.wait_for_function("() => window.okfLoomStudio.state.eventsReady")
        launcher = page.locator(".okf-studio-launcher")
        expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Editable$"))

        # Both arrive live, in order. Each toast is the proof its event was
        # handled; neither leaves anything unread.
        _append_sharing_event(linked_bundle, "tunnel_attach", "public tunnel attached: https://shared.example")
        expect(page.locator(".okf-toast", has_text="public tunnel attached: https://shared.example")).to_be_visible()
        _append_sharing_event(linked_bundle, "tunnel_detach", "public tunnel detached: https://shared.example")
        toast = page.locator(".okf-toast", has_text="public tunnel detached: https://shared.example")
        expect(toast).to_be_visible()
        assert page.evaluate("() => window.okfLoomStudio.state.attention.unread.size") == 0
        expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Editable$"))
        toast.get_by_role("button", name="View", exact=True).click()
        expect(page.locator("#okf-panel-title")).to_have_text("Agent activity")
        panel = page.locator("#okf-panel")
        sharing = page.locator(".okf-agent-sharing")
        expect(sharing.get_by_role("heading")).to_have_text("Sharing (2)")
        expect(sharing.locator(".okf-change__summary")).to_have_text([
            "public tunnel detached: https://shared.example", "public tunnel attached: https://shared.example"])
        recent = panel.get_by_role("heading", name=re.compile(r"^Recent writes"))
        expect(recent).to_have_text("Recent writes · last 5 min (0)")

        # A real agent write through the running Studio is a change.
        _okf(linked_bundle, "update-section", "--bundle", str(linked_bundle), "--id", "sub/two",
             "--heading", "Details", "--body", "An agent write beside the sharing events.", "--actor", "agent")
        page.get_by_role("button", name="Close panel").click()
        expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+\d+ changes?\b"))
        launcher.click()
        panel.get_by_role("button", name="Changes", exact=False).first.click()
        expect(page.locator("#okf-panel-title")).to_have_text("Changes")
        body = page.locator("#okf-panel-body")
        expect(body.locator(".okf-change", has_text="sub/two").first).to_be_visible()
        expect(body).not_to_contain_text("public tunnel")
        page.get_by_role("button", name="Back to Studio").click()
        panel.get_by_role("button", name="Agent activity", exact=False).first.click()
        expect(recent).to_have_text("Recent writes · last 5 min (1)")
        expect(sharing.locator(".okf-change")).to_have_count(2)


def test_sharing_is_never_evidence_that_an_agent_acted(linked_bundle, page):
    """Sharing is recorded with actor ``cli`` because the share command cannot
    tell a person from an agent. Sessions recorded before that labelled the
    same rows ``agent``, and the session feed outlives a restart. Neither kind
    of row makes the availability line say an agent acted."""
    with live_server(linked_bundle) as base:
        _studio_ready(page, base + "/sub/one")
        page.wait_for_function("() => window.okfLoomStudio.state.eventsReady")
        _append_sharing_event(linked_bundle, "tunnel_attach", "public tunnel attached: https://shared.example")
        expect(page.locator(".okf-toast", has_text="public tunnel attached: https://shared.example")).to_be_visible()
        lost = "public tunnel lost after cloudflared exited: https://shared.example"
        _append_sharing_event(linked_bundle, "tunnel_detach", lost, legacy_actor="agent")
        toast = page.locator(".okf-toast", has_text=lost)
        expect(toast).to_be_visible()
        toast.get_by_role("button", name="View", exact=True).click()
        expect(page.locator("#okf-panel-title")).to_have_text("Agent activity")
        expect(page.locator(".okf-agent-sharing .okf-change")).to_have_count(2)
        line = page.locator("#okf-panel .okf-agent-availability")
        expect(line).to_have_attribute("data-level", "none")
        expect(line).to_have_text(
            "No agent has acted in this session. Comments are saved and wait until an external agent picks them up.")


def test_comment_only_session_keeps_its_word_offline_and_promises_no_presence(comment_only_base, page):
    """R3: away from connected the launcher keeps the session's authority word,
    so a comment-only session is not called read-only; and a session that
    cannot read presence is not promised reported status changes."""
    _studio_ready(page, comment_only_base + "/sub/one")
    launcher = page.locator(".okf-studio-launcher")
    expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Comment-only$"))
    launcher.click()
    panel = page.locator("#okf-panel")
    panel.get_by_role("button", name="Agent activity", exact=False).first.click()
    sentence = "Evidence of recent agent work: comments an agent claimed and agent writes in the last 5 minutes."
    expect(page.locator("#okf-panel-purpose")).to_have_text(sentence)
    expect(panel).to_have_accessible_description(sentence)
    expect(panel.get_by_role("heading", name=re.compile("Presence history"))).to_have_count(0)
    page.get_by_role("button", name="Close panel").click()

    page.evaluate("""() => {
      const Real = window.EventSource;
      window.EventSource = function () { throw new TypeError('unsupported'); };
      try { window.okfLoomLive._connectForTest(); } finally { window.EventSource = Real; }
    }""")
    assert page.evaluate("window.okfLoomLive.state") == "offline"
    expect(launcher).to_have_attribute("data-attention", "failure")
    expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Comment-only\s+·\s+Offline\b"))


# ---- Review in Studio from a portable export --------------------------------

def _export(linked_bundle, tmp_path, target, review_base, *, include_portable_feedback=False):
    """Build a real export from a copy, so the live bundle is never touched."""
    import shutil
    source = tmp_path / f"export-src-{target}"
    shutil.copytree(linked_bundle, source, ignore=shutil.ignore_patterns(".okf-loom"))
    if review_base:
        (source / "okf-loom.config.yaml").write_text(f"viewer:\n  review_base_url: {review_base}\n", encoding="utf-8")
    out = tmp_path / f"export-{target}"
    build_site(Bundle.load(source), out, target=target, include_portable_feedback=include_portable_feedback)
    return out


def _review(page, document=None):
    link = page.locator(".okf-static-viewer-note[data-okf-review='enabled'] .okf-review__link")
    if document:
        expect(link).to_have_attribute("data-okf-review-document", document)
    else:
        expect(link).not_to_have_attribute("data-okf-review-document", re.compile("."))
    return link


def _assert_credential_free(page, link, live):
    href = link.get_attribute("href")
    assert href.startswith(live + "/") and not any(mark in href for mark in "?@")
    assert link.get_attribute("rel") == "noopener noreferrer"
    assert link.get_attribute("referrerpolicy") == "no-referrer"
    assert "Review in Studio" in link.get_attribute("aria-label")  # label in name
    assert link.get_attribute("target") is None


def test_static_export_review_links_open_the_page_on_screen_in_the_live_studio(linked_bundle, tmp_path, page):
    with live_server(linked_bundle) as live:
        out = _export(linked_bundle, tmp_path, "static", live + "/")
        port = free_port()
        site = f"http://127.0.0.1:{port}"
        argv = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(out)]
        arrivals = []
        page.on("request", lambda r: arrivals.append(r.headers) if r.is_navigation_request() and r.url.startswith(live) else None)
        with managed_server(argv, site, tmp_path / "review-http.log"):
            # A concept, at the heading in the address bar.
            page.goto(f"{site}/sub/two.html#details", wait_until="load")
            link = _review(page, "sub/two.md")
            assert link.get_attribute("href") == review_url(live + "/", "sub/two.md", "details")
            _assert_credential_free(page, link, live)
            expect(page.locator(".okf-review__where")).to_contain_text("sub/two.md")
            link.click()
            page.wait_for_url(f"{live}/sub/two.md#details")
            page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
            expect(page.get_by_role("button", name="Comment on this page", exact=True)).to_be_visible()
            assert arrivals and all("referer" not in headers for headers in arrivals)

            # Reserved pages and both index kinds carry their own document; keyboard reaches the link.
            for path, document, landed in (("/sub/log.html", "sub/log.md", 'article[data-okf-document="sub/log.md"]'),
                                           ("/sub/index.html", "sub/index.md", "main.okf-index"),
                                           ("/index.html", "index.md", "main.okf-index")):
                page.goto(site + path, wait_until="load")
                link = _review(page, document)
                assert link.get_attribute("href") == review_url(live + "/", document)
                link.focus()
                page.keyboard.press("Enter")
                page.wait_for_url(f"{live}/{document}")
                expect(page.locator(landed)).to_be_visible()

            # The graph has no document until a concept is selected, then follows it.
            page.goto(f"{site}/__graph.html", wait_until="load")
            link = _review(page)
            assert link.get_attribute("href") == live + "/"
            _select(page, f"{site}/__graph.html?selected=sub/one")
            assert _review(page, "sub/one.md").get_attribute("href") == f"{live}/sub/one.md"
            _link(page, "sibling-link").click()
            assert _review(page, "sub/two.md").get_attribute("href") == f"{live}/sub/two.md"
        assert page.failed_documents == []


def test_single_file_review_link_follows_the_concept_and_reserved_document_on_screen(linked_bundle, tmp_path, page):
    with live_server(linked_bundle) as live:
        out = _export(linked_bundle, tmp_path, "single-file", live + "/")
        url = (out / "index.html").as_uri()
        _select(page, url + "?selected=sub/one")
        link = _review(page, "sub/one.md")
        _assert_credential_free(page, link, live)

        _link(page, "dir-log-link").click()
        page.locator("#detail-document").wait_for(state="visible")
        assert _review(page, "sub/log.md").get_attribute("href") == f"{live}/sub/log.md"
        page.get_by_role("button", name="Back to One").press("Enter")
        _review(page, "sub/one.md")

        # The synthesized concept list is not a document: the link goes home.
        page.goto(url + "#okf-single-index", wait_until="load")
        page.wait_for_function("() => document.body.dataset.okfSingleDestination === 'index'")
        assert _review(page).get_attribute("href") == live + "/"

        _select(page, url + "?selected=sub/two", "sub/two")
        _review(page, "sub/two.md").click()
        page.wait_for_url(f"{live}/sub/two.md")
        expect(page.get_by_role("button", name="Comment on this page", exact=True)).to_be_visible()


@pytest.mark.parametrize("target", ["static", "single-file"])
@pytest.mark.parametrize("tampered", [None, "javascript:alert(1)", "https://user:secret@review.test/", "https://review.test/?token=s"])
def test_export_without_a_trusted_review_base_stays_readable_and_says_why(linked_bundle, tmp_path, page, target, tampered):
    out = _export(linked_bundle, tmp_path, target, None)
    entry = out / ("sub/one.html" if target == "static" else "index.html")
    if tampered:
        # A hand-edited export must not be able to mint a Studio destination.
        html = entry.read_text(encoding="utf-8")
        assert html.count('"base_url": null') == 1
        entry.write_text(html.replace('"base_url": null', '"base_url": ' + json.dumps(tampered)), encoding="utf-8")
    page.goto(entry.as_uri() + ("" if target == "static" else "?selected=sub/one"), wait_until="load")
    note = page.locator(".okf-static-viewer-note[data-okf-review='unavailable']")
    expect(note).to_be_visible()
    expect(note).to_contain_text("Review in Studio is unavailable")
    expect(page.locator(".okf-review__link")).to_have_count(0)
    assert page.locator("a[href*='review.test'], a[href^='javascript:']").count() == 0
    body = page.locator(".okf-page__body" if target == "static" else "#detail-body")
    expect(body).to_contain_text("sibling-link")
    if target == "static" and not tampered:
        # On a phone the default (no base) explanation must not push the document
        # down: it stays in the note for assistive technology and takes no room.
        page.set_viewport_size({"width": 390, "height": 844})
        box = page.locator(".okf-review--unavailable").bounding_box()
        assert box["width"] <= 1 and box["height"] <= 1
        assert "Review in Studio is unavailable" in note.evaluate("n => n.textContent")


# ---- Every selection gets a reliable target or an explicit fallback ---------

RICH = """---
type: Note
title: Rich surfaces
description: Metadata sentence here.
---

# Rich surfaces

First paragraph alpha words.

Second paragraph beta words.

```python
print("code gamma")
```

| Col | Val |
|---|---|
| delta cell | 1 |

![teal picture](pic.svg)
"""
# Misnested emphasis renders as markup the browser repairs into a different
# tree, so the server projection fails closed for the page (complete=false).
DEGRADED = "---\ntype: Note\ntitle: Degraded\n---\n\n# Degraded\n\nPlain zeta words here.\n\nMisnested **a *b** c* emphasis.\n"
# Escaped pipes (in and out of a code span), an astral character, a duplicate
# of the same visible text in the next cell, and ordinary backslashes.
PIPES = "---\ntype: Note\ntitle: Pipes\n---\n\n# Pipes\n\n| Key | Meaning |\n|---|---|\n| `x\\|y😀` | x\\|y |\n| `c\\d` | a\\\\b |\n"
UNMAPPED = "this part of the page is not mapped to the saved Markdown"
INEXACT = "the selection does not map to one exact passage of the saved Markdown"
# R3: an exact selection is named as one in the composer, never left unsaid.
EXACT_NOTE = ("Exact annotation: pinned to this passage and, once sent, highlighted on the page. It is re-checked "
              "when the page changes.")
PAGE_NOTE = "Page comment: about the whole page. No text is highlighted."


@pytest.fixture
def rich_bundle(tmp_path):
    root = tmp_path / "rich-bundle"
    root.mkdir()
    (root / "index.md").write_text("# Home\n\nOverview.\n", encoding="utf-8")
    (root / "pic.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="teal"/></svg>',
        encoding="utf-8")
    (root / "rich.md").write_text(RICH, encoding="utf-8")
    (root / "degraded.md").write_text(DEGRADED, encoding="utf-8")
    return root


def _concept_ready(page, url):
    # Three things replace a code block's nodes shortly after load, and each
    # collapses a selection made in that instant: content enhancement moving it
    # into its scrollport, syntax highlighting when its CDN module arrives, and
    # the Studio's first document commit. Fail the optional dependency so the run
    # is offline, and wait for each owner's terminal signal instead of a sleep.
    page.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
    _studio_ready(page, url)
    page.wait_for_function("() => window.okfLoomStudio.state.selectionAffordanceReady === true")
    page.wait_for_function("() => !['', 'loading', undefined].includes(document.body.dataset.okfRenderersReady)")
    page.wait_for_function("() => !!window.okfLoomStudio.state.doc")


def _select_words(page, selector, start, end=None):
    """Select from `start` to the end of `end` (or of `start`) under one root."""
    page.locator(selector).first.evaluate(
        """(root, [start, end]) => {
          const nodes = []; let text = "", node;
          const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          while ((node = walker.nextNode())) { nodes.push({node, at: text.length}); text += node.nodeValue; }
          const from = text.indexOf(start), to = end ? text.indexOf(end) + end.length : from + start.length;
          if (from < 0 || to <= from) throw new Error("selection text is absent");
          const point = (offset) => { for (const n of nodes) if (offset <= n.at + n.node.nodeValue.length) return [n.node, offset - n.at]; };
          const range = document.createRange();
          range.setStart(...point(from)); range.setEnd(...point(to));
          const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }""", [start, end])


def _comment_on_selection(page, base, body):
    action = page.locator(".okf-comment-afford")
    expect(action).to_be_visible()
    action.get_by_role("button").first.click()
    box = page.get_by_role("textbox", name="Comment for agent")
    expect(box).to_be_focused()
    note = page.locator(".okf-composer__anchor-note")
    shown = note.inner_text() if note.is_visible() else ""
    box.fill(body)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")
    _saved_card(page, body)
    return shown, next(r for r in _rows(page, base) if r["body"] == body)


def test_each_kind_of_selection_is_pinned_exactly_or_kept_as_explained_page_context(rich_bundle, page):
    exact = [("paragraph", ".okf-page__body", "paragraph alpha", None, "paragraph"),
             ("code", ".okf-page__body pre", "code gamma", None, "fenced-code"),
             ("table cell", ".okf-page__body table", "delta cell", None, "table-row")]
    fallback = [("title", ".okf-page__title", "Rich surfaces", None, "page", UNMAPPED),
                ("metadata", "article.okf-page__main", "Metadata sentence", None, "page", UNMAPPED),
                ("cross-block", ".okf-page__body", "alpha words.", "Second paragraph", "rendered", INEXACT),
                ("two table cells", ".okf-page__body table", "cell", "1", "rendered", INEXACT)]
    with live_server(rich_bundle) as base:
        for name, selector, start, end, block in exact:
            _concept_ready(page, f"{base}/rich")
            _select_words(page, selector, start, end)
            shown, row = _comment_on_selection(page, base, f"exact {name}")
            assert shown == EXACT_NOTE and row["detail"] == {}
            anchor = row["anchor"]
            assert (anchor["schema"], anchor["kind"], anchor["exact"]) == (2, "text-quote", start)
            assert anchor["block"]["id"].startswith(block + ":")

        for name, selector, start, end, surface, reason in fallback:
            _concept_ready(page, f"{base}/rich")
            _select_words(page, selector, start, end)
            shown, row = _comment_on_selection(page, base, f"fallback {name}")
            # Said before sending, and stored as a page comment: never a guessed span.
            assert shown.startswith("Page comment.") and reason in shown
            assert row["concept"] == "rich" and row["anchor"] == {"kind": "concept"}
            kept = row["detail"]["selection"]
            assert (kept["surface"], kept["precision"], kept["reason"], kept["truncated"]) == (surface, "page", reason, False)
            assert kept["quote"].startswith(start) and (end is None or kept["quote"].endswith(end))
            # Only the two exact comments above are marked in the text.
            expect(page.locator(".okf-page__body mark")).to_have_count(len(exact))

        # An image has no text to select: nothing is offered and nothing is lost,
        # because the page comment stays one control away.
        _concept_ready(page, f"{base}/rich")
        page.locator(".okf-page__body img").evaluate(
            "img => { const r = document.createRange(); r.selectNode(img); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }")
        page.wait_for_timeout(400)  # longer than the 120 ms selection debounce
        expect(page.locator(".okf-comment-afford")).to_be_hidden()
        expect(page.get_by_role("button", name="Comment on this page", exact=True)).to_be_visible()


def _selection(page):
    return page.evaluate("() => getSelection().toString()")


def test_a_comment_refresh_keeps_a_selection_that_begins_inside_a_highlight(rich_bundle, page):
    """Refreshing marks rewrites their text nodes; the reader's selection stays put.

    Regression: the live handshake re-fetches comments just after load. A
    selection that began inside an existing highlight was relocated to the start
    of that highlight, so "alpha words." silently became "paragraph alpha
    words." in whatever was captured next - exact or advisory.
    """
    with live_server(rich_bundle) as base:
        _concept_ready(page, f"{base}/rich")
        _select_words(page, ".okf-page__body", "paragraph alpha")
        _comment_on_selection(page, base, "exact paragraph")
        _concept_ready(page, f"{base}/rich")
        marks = page.locator(".okf-page__body mark.okf-comment-mark")
        expect(marks).to_have_text(["paragraph alpha"])

        # Forward across two blocks, starting inside the highlight. The refresh
        # is the real canonical reload, awaited - not a timer.
        _select_words(page, ".okf-page__body", "alpha words.", "Second paragraph")
        page.evaluate("() => window.okfLoomStudio._loadComments()")
        assert _selection(page) == "alpha words.\n\nSecond paragraph"
        expect(marks).to_have_text(["paragraph alpha"])

        # Backward, wholly inside the highlight: direction survives too.
        page.evaluate("""() => {
          const text = document.querySelector('.okf-page__body mark.okf-comment-mark').firstChild;
          getSelection().setBaseAndExtent(text, 15, text, 10);
        }""")
        page.evaluate("() => window.okfLoomStudio._loadComments()")
        assert _selection(page) == "alpha"
        assert page.evaluate("""() => { const s = getSelection();
          return s.anchorNode === s.focusNode ? s.anchorOffset > s.focusOffset
            : !!(s.anchorNode.compareDocumentPosition(s.focusNode) & Node.DOCUMENT_POSITION_PRECEDING); }""")

        # What is then captured is what the reader selected.
        _select_words(page, ".okf-page__body", "alpha words.", "Second paragraph")
        page.evaluate("() => window.okfLoomStudio._loadComments()")
        _, row = _comment_on_selection(page, base, "cross-block after a refresh")
        assert row["detail"]["selection"]["quote"] == "alpha words.\n\nSecond paragraph"


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_table_cells_with_escaped_pipes_are_pinned_exactly_and_highlighted_after_reload(tmp_path, page, newline):
    root = tmp_path / "pipes-bundle"
    root.mkdir()
    (root / "index.md").write_bytes(b"# Home\n")
    source = PIPES.replace("\n", newline)
    (root / "pipes.md").write_bytes(source.encode("utf-8"))
    row_one, row_two = ".okf-page__body tbody tr:nth-child(1)", ".okf-page__body tbody tr:nth-child(2)"
    picks = [("emoji suffix", f"{row_one} td:nth-child(1)", "y😀", "y😀"),
             ("second x|y", f"{row_one} td:nth-child(2)", "x|y", "x\\|y"),
             ("code backslash", f"{row_two} td:nth-child(1)", "c\\d", "c\\d")]
    with live_server(root) as base:
        for name, selector, visible, exact in picks:
            _concept_ready(page, f"{base}/pipes")
            _select_words(page, selector, visible)
            shown, row = _comment_on_selection(page, base, f"pinned {name}")
            anchor = row["anchor"]
            assert shown == EXACT_NOTE and row["detail"] == {}
            assert (anchor["schema"], anchor["kind"], anchor["exact"]) == (2, "text-quote", exact)
            assert anchor["block"]["id"].startswith("table-row:")
            # Offsets are Unicode code points into the saved bytes, newline intact.
            assert source[anchor["position"]["start"]:anchor["position"]["end"]] == exact
        rows = {r["body"]: r["anchor"] for r in _rows(page, base)}
        # The duplicate is the SECOND x|y of the row, after the code cell's.
        assert rows["pinned second x|y"]["position"]["start"] > source.index("x\\|y") + 4
        assert rows["pinned emoji suffix"]["suffix"].startswith("` | x\\|y")

        # Durable: a fresh load resolves every row and highlights the same text
        # in the same cell. Marks show the rendered "|", never the source "\|".
        _concept_ready(page, f"{base}/pipes")
        marks = page.locator(".okf-page__body mark.okf-comment-mark")
        expect(marks).to_have_text(["y😀", "x|y", "c\\d"])
        expect(page.locator(f"{row_one} td:nth-child(2) mark")).to_have_text("x|y")
        expect(page.locator(f"{row_one} td:nth-child(1) code mark")).to_have_text("y😀")
        assert page.locator(f"{row_one}").inner_text().replace("\t", " ") == "x|y😀 x|y"

        # The card's Jump lands on its own highlight.
        page.locator(".okf-studio-launcher").click()
        page.locator("#okf-panel").get_by_role("button", name="Comments", exact=True).click()
        _saved_card(page, "pinned second x|y").get_by_role("button", name="Jump", exact=True).click()
        assert page.evaluate("() => document.activeElement.closest('td') === document.querySelector('%s td:nth-child(2)')" % row_one)
        assert page.evaluate("() => document.activeElement.textContent") == "x|y"

        # A selection through two cells is not one source passage: page comment.
        _concept_ready(page, f"{base}/pipes")
        _select_words(page, row_one, "y😀", "😀x|y")
        shown, row = _comment_on_selection(page, base, "across two cells")
        assert shown.startswith("Page comment.") and INEXACT in shown
        assert row["anchor"] == {"kind": "concept"} and row["detail"]["selection"]["precision"] == "page"

        # The decoder accepts exactly one transformation. Forged maps reject.
        verdicts = page.evaluate(r"""() => {
          const api = window.okfLoomStudio, doc = api.state.doc, source = doc.annotation_source;
          const cps = Array.from(source);
          const check = (mutate) => { const p = JSON.parse(JSON.stringify(doc.annotation_projection)); mutate(p);
            return api._projectionError(p, doc.concept_content_rev, source, doc.html); };
          const rowWith = (p, raw) => p.blocks.find(b => b.segments.some(s => cps.slice(s.source_start, s.source_end).join('') === raw));
          return {
            genuine: check(() => {}),
            // An ordinary code backslash split off as a forged 2-to-1 "escape".
            backslash: check(p => { const b = rowWith(p, 'c\\d'); const i = b.segments.findIndex(s => cps.slice(s.source_start, s.source_end).join('') === 'c\\d');
              const s = b.segments[i]; const rest = b.segments.slice(i + 1).map(x => Object.assign({}, x, {dom_start: x.dom_start - 1, dom_end: x.dom_end - 1}));
              b.segments = b.segments.slice(0, i).concat([
                {dom_start: s.dom_start, dom_end: s.dom_start + 1, source_start: s.source_start, source_end: s.source_start + 1},
                {dom_start: s.dom_start + 1, dom_end: s.dom_start + 2, source_start: s.source_start + 1, source_end: s.source_end}], rest); }),
            // The genuine escape re-pointed at two other source code points.
            repointed: check(p => { const b = rowWith(p, '\\|'); const s = b.segments.find(s => cps.slice(s.source_start, s.source_end).join('') === '\\|');
              s.source_start -= 1; s.source_end -= 1; }),
            // The same escape claimed by a block that is not a table row.
            wrongBlock: check(p => { rowWith(p, '\\|').block_id = 'paragraph:5'; }),
          };
        }""")
        assert verdicts["genuine"] is None, verdicts
        assert all(verdicts[k] for k in ("backslash", "repointed", "wrongBlock")), verdicts


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_list_continuation_dom_capture_keeps_exact_source_offsets(tmp_path, page, newline):
    root = tmp_path / "lists"
    root.mkdir()
    (root / "index.md").write_text("# Home\n")
    source = ("---\ntype: Note\n---\n\n"
              "- one😀\n  two\n- parent\n  - child\n  - last\n").replace("\n", newline)
    (root / "note.md").write_bytes(source.encode())
    with live_server(root) as base:
        for selected in ("😀", "two", "parent"):
            _concept_ready(page, f"{base}/note")
            # The browser holds the exact saved bytes: CRLF survives to the
            # client only in the crlf case, so the offsets below differ.
            assert page.evaluate(
                "() => window.okfLoomStudio.state.doc.annotation_source.includes('\\r\\n')"
            ) is (newline == "\r\n")
            _select_words(page, ".okf-page__body > ul", selected)
            shown, row = _comment_on_selection(page, base, f"list {selected}")
            assert shown == EXACT_NOTE and row["detail"] == {}
            anchor = row["anchor"]
            assert (anchor["schema"], anchor["kind"], anchor["exact"]) == (2, "text-quote", selected)
            assert anchor["block"]["id"].startswith("list-item:")
            assert anchor["position"] == {"start": source.index(selected),
                                          "end": source.index(selected) + len(selected)}
        _concept_ready(page, f"{base}/note")
        assert page.locator(".okf-page__body > ul > li").all_text_contents() == [
            "one😀\ntwo", "parent\nchild\nlast"]
        # R5: a nested child item now resolves to its existing verified
        # enclosing top-level list-item owner (the only keyed li), LF and
        # CRLF alike — no new keys, the owner record was already complete.
        _select_words(page, ".okf-page__body > ul", "child")
        shown, row = _comment_on_selection(page, base, "nested child exact")
        assert shown == EXACT_NOTE and row["detail"] == {}
        anchor = row["anchor"]
        assert (anchor["schema"], anchor["kind"], anchor["exact"]) == (2, "text-quote", "child")
        assert anchor["block"]["id"].startswith("list-item:")
        assert anchor["position"] == {"start": source.index("child"),
                                      "end": source.index("child") + len("child")}
        # Selections that cross the nested boundary keep the honest fallback:
        # the source between an item and its children carries the "  - "
        # marker gap, so no contiguous canonical span exists to save.
        for name, start, end in (
            ("across top-level items", "two", "parent"),
            ("outer into child", "parent", "child"),
            ("across nested siblings", "child", "last"),
        ):
            _concept_ready(page, f"{base}/note")
            _select_words(page, ".okf-page__body > ul", start, end)
            shown, row = _comment_on_selection(page, base, f"nested gap {name}")
            assert shown.startswith("Page comment.") and INEXACT in shown, name
            assert row["anchor"] == {"kind": "concept"}, name
        # Saved child anchor re-marks the nested text through the projection.
        _concept_ready(page, f"{base}/note")
        expect(page.locator(".okf-page__body mark.okf-comment-mark")).to_have_text(
            ["😀", "two", "parent", "child"])


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_nested_children_pin_the_enclosing_owner_across_depth_markup_and_repeats(tmp_path, page, newline):
    """R5 narrow ownership: deeper nesting, inline markup, and repeated child
    text all resolve to the same existing keyed top-level list item, with
    canonical saved positions and visible marks through public persistence."""
    root = tmp_path / "nested-deep"
    root.mkdir()
    (root / "index.md").write_text("# Home\n")
    source = ("---\ntype: Note\n---\n\n"
              "- trunk\n  - **bold branch**\n  - twig\n    - twig\n- other\n").replace("\n", newline)
    (root / "note.md").write_bytes(source.encode())
    with live_server(root) as base:
        # Inline markup inside a nested child: the saved span covers exactly
        # the visible words (not the ** markers), owned by the keyed root li.
        _concept_ready(page, f"{base}/note")
        _select_words(page, ".okf-page__body > ul", "bold branch")
        shown, row = _comment_on_selection(page, base, "deep inline markup")
        assert shown == EXACT_NOTE and row["detail"] == {}
        anchor = row["anchor"]
        assert (anchor["kind"], anchor["exact"]) == ("text-quote", "bold branch")
        assert anchor["block"]["id"].startswith("list-item:")
        assert anchor["position"] == {"start": source.index("bold branch"),
                                      "end": source.index("bold branch") + len("bold branch")}
        # Repeated child text: the deeper occurrence is chosen, not the first
        # match — selection boundaries decide the occurrence.
        _concept_ready(page, f"{base}/note")
        _select_words(page, ".okf-page__body > ul > li > ul > li > ul > li", "twig")
        shown, row = _comment_on_selection(page, base, "deep repeated child")
        assert shown == EXACT_NOTE and row["detail"] == {}
        anchor = row["anchor"]
        assert (anchor["kind"], anchor["exact"]) == ("text-quote", "twig")
        deep_at = source.index("twig", source.index("twig") + 1)
        assert anchor["position"] == {"start": deep_at, "end": deep_at + len("twig")}
        # Saved anchors re-mark the nested text after a fresh canonical load.
        _concept_ready(page, f"{base}/note")
        expect(page.locator(".okf-page__body mark.okf-comment-mark")).to_have_text(
            ["bold branch", "twig"])


def test_self_fragment_keeps_live_document_and_unsent_draft(linked_bundle, page):
    path = linked_bundle / "sub/one.md"
    path.write_text(ONE + "\n[self-link](#tail) [relative-self](./one.md#tail) "
                    "[absolute-self](/sub/one.md#tail)\n\n"
                    + "\n\n".join(f"Paragraph {i}." for i in range(50)) + "\n\n## Tail\n")
    with live_server(linked_bundle) as base:
        _concept_ready(page, f"{base}/sub/one")
        page.get_by_role("button", name="Comment on this page", exact=True).click()
        box = page.get_by_role("textbox", name="Comment for agent")
        box.fill("Unsent draft stays on this document")
        page.evaluate("() => { window.__originalBody = document.querySelector('.okf-page__body'); }")
        documents = []
        page.on("request", lambda r: documents.append(r.url) if r.resource_type == "document" else None)
        for label in ("self-link", "relative-self", "absolute-self"):
            link = page.get_by_role("link", name=label, exact=True)
            expect(link).to_have_attribute("href", "#tail")
            link.click()
            page.wait_for_url(f"{base}/sub/one#tail")
            page.wait_for_function("() => document.querySelector('#tail').matches(':target')")
            assert page.evaluate("() => window.__originalBody === document.querySelector('.okf-page__body')")
            expect(box).to_have_value("Unsent draft stays on this document")
            page.go_back()
            page.wait_for_url(f"{base}/sub/one")
        assert documents == [] and len(page.context.pages) == 1
        assert _rows(page, base) == []


def test_a_page_whose_source_map_is_incomplete_still_loads_and_takes_page_comments(rich_bundle, page):
    with live_server(rich_bundle) as base:
        _concept_ready(page, f"{base}/degraded")  # asserts commentsStatus == ready
        # Said up front, before anything is typed: text cannot be pinned here.
        page.get_by_role("button", name="Comment on this page", exact=True).click()
        expect(page.locator(".okf-composer__anchor-note")).to_contain_text("Page comments only")
        expect(page.locator(".okf-composer__anchor-note")).to_contain_text("Selected text is quoted in the comment.")
        _comment_here(page, "Plain page comment on a degraded page")
        assert next(r for r in _rows(page, base) if r["body"].startswith("Plain page"))["anchor"] == {"kind": "concept"}

        _select_words(page, ".okf-page__body", "zeta words")
        shown, row = _comment_on_selection(page, base, "Selected on a degraded page")
        assert shown.startswith("Page comment.") and "zeta words" in shown
        assert "is quoted in the comment, not attached to exact text" in shown
        assert row["anchor"] == {"kind": "concept"}
        assert row["detail"]["selection"]["quote"] == "zeta words"
        assert row["detail"]["selection"]["precision"] == "page"


def test_a_surface_with_no_document_says_so_and_sends_nothing(linked_bundle, page):
    with live_server(linked_bundle) as base:
        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/__comment") else None)
        _studio_ready(page, f"{base}/__graph")
        expect(page.get_by_role("group", name="Concept feedback")).to_be_hidden()
        page.locator(".okf-studio-launcher").click()
        page.locator("#okf-panel").get_by_role("button", name="Comments", exact=True).click()
        note = page.locator(".okf-composer__anchor-note")
        expect(note).to_have_text("Open a document, or select a concept in the graph, to comment on it.")
        box = page.get_by_role("textbox", name="Comment for agent")
        box.fill("Nowhere to put this")
        send = page.get_by_role("button", name="Send", exact=True)
        expect(send).to_be_disabled()
        box.press("Control+Enter")  # the keyboard route is closed too
        expect(box).to_have_value("Nowhere to put this")  # the draft is kept
        assert posts == [] and _rows(page, base) == []

        # Selecting a concept gives the same draft a real target.
        page.evaluate("() => window.okfActivateGraphConcept('sub/one', 'keyboard')")
        page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="visible")
        expect(note).to_have_text(PAGE_NOTE)
        expect(page.locator(".okf-composer__anchor code")).to_have_text("sub/one")
        expect(send).to_be_enabled()
        send.click()
        expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")
        assert [(r["concept"], r["body"]) for r in _rows(page, base)] == [("sub/one", "Nowhere to put this")]


# ---- Graph draft target ownership ------------------------------------------
# A graph draft with a saved target and something worth keeping (text, selected
# text, or a post still being confirmed) stays on that target when the
# selection changes; only Move, Cancel or a successful post change it.


def _comment_posts(page):
    posts = []
    page.on("request", lambda r: posts.append(r.url)
            if r.method == "POST" and r.url.endswith("/__comment") else None)
    return posts


def _graph_ready(page, base, concept="sub/one"):
    _studio_ready(page, f"{base}/__graph?selected={concept}")
    page.wait_for_selector(f'#detail-body[data-concept-id="{concept}"]', state="attached")


def _row_for(page, base, body):
    row = next(r for r in _rows(page, base) if r["body"] == body)
    return row["concept"], row.get("document", ""), row["anchor"], row["detail"]


def _posted(page):
    expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")


def test_graph_typed_draft_keeps_its_concept_until_moved(linked_bundle, page):
    with live_server(linked_bundle) as base:
        posts = _comment_posts(page)
        _graph_ready(page, base)
        page.get_by_role("button", name="Comment on this concept", exact=True).click()
        box = page.get_by_role("textbox", name="Comment for agent")
        expect(box).to_be_focused()
        box.fill("One needs a summary")
        anchor = page.locator(".okf-composer__anchor code")
        group = page.get_by_role("group", name="Draft target")
        note = page.locator("#okf-composer-ownership-note")
        expect(group).to_be_hidden()

        # Selecting another concept does not carry the typed draft with it.
        _link(page, "sibling-link").click()
        page.wait_for_selector('#detail-body[data-concept-id="sub/two"]', state="visible")
        expect(anchor).to_have_text("sub/one")
        expect(box).to_have_value("One needs a summary")
        expect(note).to_have_text("Your unsent comment stays on sub/one.")
        move = group.get_by_role("button", name="Move draft to sub/two", exact=True)
        expect(move).to_be_visible()
        expect(page.locator("#okf-annotation-status")).to_have_text(
            "Your unsent comment stays on sub/one. It was not moved to sub/two.")
        assert box.get_attribute("aria-describedby").split() == ["okf-composer-ownership-note", "okf-composer-purpose"]
        move.focus()
        page.keyboard.press("Tab")  # Move comes straight before the text it moves
        expect(box).to_be_focused()

        # sub/two's own entry reopens the same draft, still on sub/one.
        page.get_by_role("button", name="Comment on this concept", exact=True).click()
        expect(box).to_be_focused()
        expect(anchor).to_have_text("sub/one")
        page.get_by_role("button", name="Send", exact=True).click()
        _posted(page)
        assert _row_for(page, base, "One needs a summary")[0] == "sub/one"
        assert len(posts) == 1
        # Sent: the empty composer follows the selection again.
        expect(box).to_have_value("")
        expect(anchor).to_have_text("sub/two")
        expect(group).to_be_hidden()
        assert box.get_attribute("aria-describedby") == "okf-composer-purpose"

        # Typed on sub/two, kept there when sub/one is selected, until Move.
        box.fill("Actually about one")
        page.evaluate("() => window.okfActivateGraphConcept('sub/one', 'keyboard')")
        page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="visible")
        expect(anchor).to_have_text("sub/two")
        move = group.get_by_role("button", name="Move draft to sub/one", exact=True)
        move.focus()
        page.keyboard.press("Enter")
        expect(anchor).to_have_text("sub/one")
        expect(group).to_be_hidden()
        expect(box).to_be_focused()
        expect(box).to_have_value("Actually about one")
        page.keyboard.press("Enter")
        _posted(page)
        assert _row_for(page, base, "Actually about one")[0] == "sub/one"
        assert len(posts) == 2


def _show_graph_concept(page, concept):
    page.evaluate("(id) => window.okfActivateGraphConcept(id, 'keyboard')", concept)
    page.wait_for_selector(f'#detail-body[data-concept-id="{concept}"]', state="visible")
    # The shown-target watcher runs on the pane's mutation; an announcement it
    # makes lands on the next frame. Two frames later it has landed, or never will.
    page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")


def _record_announcements(page):
    page.evaluate("""() => {
      window.__announced = []; let last = "";
      new MutationObserver(() => {
        const region = document.getElementById("okf-annotation-status");
        const text = region ? region.textContent : "";
        if (text && text !== last) window.__announced.push(text);
        last = text;
      }).observe(document.body, { subtree: true, childList: true, characterData: true });
    }""")
    return lambda: [t for t in page.evaluate("() => window.__announced") if t.startswith("Your unsent comment")]


def _until(page, predicate, message, timeout_s=15.0):
    """Poll a Python-side predicate while Playwright dispatches route events."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        page.wait_for_timeout(50)
    raise AssertionError(message)


CONTEXT_STAYS = ("Your unsent comment and its selected text stay on sub/one."
                 " Press Esc to remove the selection before moving it.")


@pytest.mark.parametrize("layout", ["rail", "overlay"])
def test_graph_context_draft_needs_escape_before_move(linked_bundle, page, layout):
    """Escape removes a draft's selected text in both layouts (in the rail it
    never closes the composer), so the note gives one instruction everywhere."""
    with live_server(linked_bundle) as base:
        posts = _comment_posts(page)
        _graph_ready(page, base)
        _select_words(page, "#detail-body", "and dot-link")
        action = page.locator(".okf-comment-afford")
        expect(action).to_be_visible()
        action.get_by_role("button").first.click()
        box = page.get_by_role("textbox", name="Comment for agent")
        expect(box).to_be_focused()
        anchor = page.locator(".okf-composer__anchor code")
        context = page.locator(".okf-composer__anchor-note")
        group = page.get_by_role("group", name="Draft target")
        note = page.locator("#okf-composer-ownership-note")
        move = page.locator(".okf-composer__move")
        validation = page.locator("#okf-composer-validation")
        panel = page.locator("#okf-panel")
        expect(context).to_contain_text("graph preview selection")
        if layout == "overlay":
            page.set_viewport_size({"width": 1024, "height": 900})
        page.wait_for_function("(rail) => document.body.classList.contains('okf-panel-rail') === rail",
                               arg=layout == "rail")

        # Selected text alone keeps an empty draft on sub/one.
        _show_graph_concept(page, "sub/two")
        expect(anchor).to_have_text("sub/one")
        expect(context).to_contain_text("graph preview selection")
        expect(note).to_have_text(CONTEXT_STAYS)
        expect(move).to_be_hidden()
        # Validation comes first in the description, then the draft-target note.
        box.focus()
        page.keyboard.press("Enter")
        expect(validation).to_have_text("Enter a comment to send.")
        assert box.get_attribute("aria-describedby").split() == [
            "okf-composer-validation", "okf-composer-ownership-note", "okf-composer-purpose"]
        page.keyboard.type("Belongs with two")
        expect(validation).to_be_hidden()
        assert box.get_attribute("aria-describedby").split() == [
            "okf-composer-ownership-note", "okf-composer-purpose"]

        # Escape removes the selected text but not the target, in either layout.
        page.keyboard.press("Escape")
        expect(panel).to_be_visible()
        expect(box).to_be_focused()
        expect(anchor).to_have_text("sub/one")
        expect(context).to_have_text(PAGE_NOTE)  # the quoted selection is gone
        expect(box).to_have_value("Belongs with two")
        expect(note).to_have_text("Your unsent comment stays on sub/one.")
        if layout == "rail":
            # Nothing is left to remove: the rail keeps native Escape and closes nothing.
            page.keyboard.press("Escape")
            expect(panel).to_be_visible()
            expect(box).to_be_focused()
            expect(box).to_have_value("Belongs with two")
        group.get_by_role("button", name="Move draft to sub/two", exact=True).click()
        expect(anchor).to_have_text("sub/two")
        expect(group).to_be_hidden()
        expect(box).to_be_focused()
        page.get_by_role("button", name="Send", exact=True).click()
        _posted(page)
        concept, _, stored_anchor, detail = _row_for(page, base, "Belongs with two")
        assert (concept, stored_anchor, detail) == ("sub/two", {"kind": "concept"}, {})
        assert len(posts) == 1


def _open_graph_comments(page):
    # The graph shell has no toolbar Comments button; Studio home leads there.
    page.locator(".okf-studio-launcher").click()
    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=True).click()
    expect(page.locator("#okf-panel-title")).to_have_text("Comments")


def _clear_graph_selection(page):
    page.locator(".okf-graph-canvas-proxy").focus()
    page.keyboard.press("Escape")
    expect(page.locator("#detail-empty")).to_be_visible()


def test_graph_draft_for_deleted_concept_is_kept_and_movable(linked_bundle, page):
    with live_server(linked_bundle) as base:
        posts = _comment_posts(page)
        _graph_ready(page, base)
        page.get_by_role("button", name="Comment on this concept", exact=True).click()
        box = page.get_by_role("textbox", name="Comment for agent")
        expect(box).to_be_focused()
        box.fill("One is gone")
        page.get_by_role("button", name="Close panel").click()
        (linked_bundle / "sub" / "one.md").unlink()
        expect(page.locator("#detail-empty")).to_be_visible(timeout=15000)

        _open_graph_comments(page)
        anchor = page.locator(".okf-composer__anchor code")
        group = page.get_by_role("group", name="Draft target")
        note = page.locator("#okf-composer-ownership-note")
        status = page.locator(".okf-composer__status")
        send = page.get_by_role("button", name="Send", exact=True)
        expect(box).to_have_value("One is gone")
        expect(anchor).to_have_text("sub/one")
        expect(note).to_have_text("Your unsent comment stays on sub/one. No concept is selected.")
        expect(page.locator(".okf-composer__move")).to_be_hidden()

        send.click()
        missing = "The comment was not posted because sub/one no longer exists. Your text is still in the composer."
        expect(status).to_have_text(missing)
        expect(send).to_be_disabled()
        expect(box).to_have_value("One is gone")
        assert len(posts) == 1 and _rows(page, base) == []
        # Editing keeps the explanation, and Enter does not re-send to the missing concept.
        box.focus()
        box.press("End")
        page.keyboard.type(" Still.")
        box.press("Enter")
        expect(status).to_have_text(missing)
        expect(send).to_be_disabled()
        assert len(posts) == 1

        page.evaluate("() => window.okfActivateGraphConcept('sub/two', 'keyboard')")
        page.wait_for_selector('#detail-body[data-concept-id="sub/two"]', state="visible")
        expect(note).to_have_text("Your unsent comment stays on sub/one.")
        group.get_by_role("button", name="Move draft to sub/two", exact=True).click()
        expect(anchor).to_have_text("sub/two")
        expect(status).to_have_count(0)
        expect(send).to_be_enabled()
        send.click()
        _posted(page)
        assert _row_for(page, base, "One is gone Still.")[0] == "sub/two"
        assert len(posts) == 2


def test_cleared_graph_selection_is_not_a_stale_comment_target(linked_bundle, page):
    """Clearing hides the preview but leaves its last concept id behind.

    The live studio has no in-graph reserved reader (a log link navigates), so
    the reserved half of this contract is the navigated log page's own target.
    """
    with live_server(linked_bundle) as base:
        posts = _comment_posts(page)
        _graph_ready(page, base)
        close = page.get_by_role("button", name="Close panel")
        box = page.get_by_role("textbox", name="Comment for agent")
        anchor = page.locator(".okf-composer__anchor code")
        note = page.locator("#okf-composer-ownership-note")
        send = page.get_by_role("button", name="Send", exact=True)
        page.get_by_role("button", name="Comment on this concept", exact=True).click()
        expect(anchor).to_have_text("sub/one")
        close.click()

        _clear_graph_selection(page)
        _open_graph_comments(page)
        expect(anchor).to_have_text("no document")
        expect(page.locator(".okf-composer__anchor-note")).to_have_text(
            "Open a document, or select a concept in the graph, to comment on it.")
        box.fill("Where does this go")
        expect(send).to_be_disabled()
        box.press("Enter")
        expect(page.locator("#okf-composer-validation")).to_have_text(
            "There is no document to comment on here. Open a document, or select a concept in the graph.")
        assert posts == []

        # A typed draft with no target takes the next selection, then keeps it.
        page.evaluate("() => window.okfActivateGraphConcept('sub/one', 'keyboard')")
        page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="visible")
        expect(anchor).to_have_text("sub/one")
        expect(note).to_be_hidden()
        close.click()
        _clear_graph_selection(page)
        _open_graph_comments(page)
        expect(anchor).to_have_text("sub/one")
        expect(note).to_have_text("Your unsent comment stays on sub/one. No concept is selected.")
        expect(page.locator(".okf-composer__move")).to_be_hidden()
        send.click()
        _posted(page)
        assert _row_for(page, base, "Where does this go")[0] == "sub/one"
        assert len(posts) == 1

        # A reserved log names itself, never the concept it was reached from.
        close.click()
        page.evaluate("() => window.okfActivateGraphConcept('sub/one', 'keyboard')")
        page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="visible")
        _link(page, "dir-log-link").click()
        page.wait_for_url(f"{base}/sub/log.md#history")
        page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.commentsStatus === 'ready'")
        page.get_by_role("button", name="Comment on this page", exact=True).click()
        expect(anchor).to_have_text("sub/log.md")
        box.fill("Log needs dates")
        send.click()
        _posted(page)
        concept, document, _, _ = _row_for(page, base, "Log needs dates")
        assert (concept, document) == ("", "sub/log.md")
        assert len(posts) == 2


def test_graph_pinned_draft_on_compact_screen(linked_bundle, page, tmp_path):
    page.set_viewport_size({"width": 390, "height": 844})
    with live_server(linked_bundle) as base:
        posts = _comment_posts(page)
        _graph_ready(page, base)
        page.evaluate("""() => {
          window.__announced = []; let last = "";
          new MutationObserver(() => {
            const region = document.getElementById("okf-annotation-status");
            const text = region ? region.textContent : "";
            if (text && text !== last) window.__announced.push(text);
            last = text;
          }).observe(document.body, { subtree: true, childList: true, characterData: true });
        }""")
        details = page.locator('[data-graph-pane="details"]')
        comment = page.get_by_role("button", name="Comment on this concept", exact=True)
        box = page.get_by_role("textbox", name="Comment for agent")
        group = page.get_by_role("group", name="Draft target")
        details.click()
        comment.click()
        expect(box).to_be_focused()
        box.fill("Two should cite one")
        page.get_by_role("button", name="Close panel").click()

        _link(page, "sibling-link").click()
        page.wait_for_selector('#detail-body[data-concept-id="sub/two"]', state="visible")
        announced = "Your unsent comment stays on sub/one. It was not moved to sub/two."
        expect(page.locator("#okf-annotation-status")).to_have_text(announced)

        details.click()
        comment.click()
        expect(box).to_have_value("Two should cite one")
        expect(page.locator(".okf-composer__anchor code")).to_have_text("sub/one")
        move = group.get_by_role("button", name="Move draft to sub/two", exact=True)
        expect(group).to_be_in_viewport()
        expect(move).to_be_visible()
        assert move.bounding_box()["height"] >= 44
        layout = page.evaluate("""() => { const p = document.getElementById("okf-panel");
          return { page: document.documentElement.scrollWidth, panel: p.scrollWidth - p.clientWidth,
                   right: p.getBoundingClientRect().right }; }""")
        assert layout["page"] <= 390 and layout["panel"] <= 0 and layout["right"] <= 390, layout
        page.screenshot(path=str(tmp_path / "graph-pinned-draft-390.png"))
        assert page.evaluate("() => window.__announced").count(announced) == 1

        move.click()
        expect(page.locator(".okf-composer__anchor code")).to_have_text("sub/two")
        expect(group).to_be_hidden()
        page.get_by_role("button", name="Send", exact=True).click()
        _posted(page)
        assert _row_for(page, base, "Two should cite one")[0] == "sub/two"
        assert len(posts) == 1


STILL_CONFIRMING = "This comment is still being confirmed on sub/one, so it can't be moved."


def test_graph_pending_post_keeps_its_concept_when_the_selection_changes(linked_bundle, page):
    with live_server(linked_bundle) as base:
        _graph_ready(page, base)
        announced = _record_announcements(page)
        held = []
        page.route("**/__comment", lambda route: held.append(route)
                   if route.request.method == "POST" else route.continue_())
        page.get_by_role("button", name="Comment on this concept", exact=True).click()
        box = page.get_by_role("textbox", name="Comment for agent")
        anchor = page.locator(".okf-composer__anchor code")
        group = page.get_by_role("group", name="Draft target")
        note = page.locator("#okf-composer-ownership-note")
        box.fill("Pending about one")
        page.get_by_role("button", name="Send", exact=True).click()
        _until(page, lambda: held, "the held comment POST")
        expect(box).to_be_disabled()

        _show_graph_concept(page, "sub/two")
        expect(anchor).to_have_text("sub/one")
        expect(note).to_have_text(STILL_CONFIRMING)
        expect(page.locator(".okf-composer__move")).to_be_hidden()
        _show_graph_concept(page, "sub/one")
        expect(group).to_be_hidden()
        _show_graph_concept(page, "sub/two")
        expect(note).to_have_text(STILL_CONFIRMING)
        # The note explains a post being confirmed; nothing is announced over it.
        expect(page.locator("#okf-annotation-status")).to_have_text("Posting comment…")
        assert announced() == []

        held[0].continue_()  # the real server stores it
        _posted(page)
        assert [(r["concept"], r["body"]) for r in _rows(page, base)] == [("sub/one", "Pending about one")]
        # Confirmed: the empty composer follows the selection again.
        expect(box).to_have_value("")
        expect(anchor).to_have_text("sub/two")
        expect(group).to_be_hidden()


def test_graph_draft_edited_after_an_unconfirmed_post_can_be_moved(linked_bundle, page):
    with live_server(linked_bundle) as base:
        posts = _comment_posts(page)
        _graph_ready(page, base)
        armed = {"on": True}
        page.route("**/__comment", lambda route: route.abort()
                   if armed["on"] and route.request.method == "POST" else route.continue_())
        page.get_by_role("button", name="Comment on this concept", exact=True).click()
        box = page.get_by_role("textbox", name="Comment for agent")
        anchor = page.locator(".okf-composer__anchor code")
        group = page.get_by_role("group", name="Draft target")
        note = page.locator("#okf-composer-ownership-note")
        box.fill("Maybe about one")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_role("button", name="Retry send", exact=True)).to_be_enabled()
        expect(box).to_have_value("Maybe about one")

        _show_graph_concept(page, "sub/two")
        expect(anchor).to_have_text("sub/one")
        expect(note).to_have_text(STILL_CONFIRMING)
        expect(page.locator(".okf-composer__move")).to_be_hidden()

        # Editing the text ends that attempt: the draft is an ordinary unsent one.
        box.focus()
        box.press("End")
        page.keyboard.type("!")
        expect(note).to_have_text("Your unsent comment stays on sub/one.")
        move = group.get_by_role("button", name="Move draft to sub/two", exact=True)
        expect(move).to_be_visible()
        armed["on"] = False
        move.click()
        expect(anchor).to_have_text("sub/two")
        page.get_by_role("button", name="Send", exact=True).click()
        _posted(page)
        assert [(r["concept"], r["body"]) for r in _rows(page, base)] == [("sub/two", "Maybe about one!")]
        assert len(posts) == 2


def test_graph_draft_for_deleted_concept_is_sent_when_it_returns(linked_bundle, page):
    with live_server(linked_bundle) as base:
        posts = _comment_posts(page)
        _graph_ready(page, base)
        page.get_by_role("button", name="Comment on this concept", exact=True).click()
        box = page.get_by_role("textbox", name="Comment for agent")
        expect(box).to_be_focused()
        box.fill("One is back")
        page.get_by_role("button", name="Close panel").click()
        source = linked_bundle / "sub" / "one.md"
        text = source.read_text(encoding="utf-8")
        source.unlink()
        expect(page.locator("#detail-empty")).to_be_visible(timeout=15000)

        _open_graph_comments(page)
        status = page.locator(".okf-composer__status")
        send = page.get_by_role("button", name="Send", exact=True)
        send.click()
        expect(status).to_have_text(
            "The comment was not posted because sub/one no longer exists. Your text is still in the composer.")
        expect(send).to_be_disabled()
        assert len(posts) == 1 and _rows(page, base) == []

        # sub/one comes back and is shown again: the hold is released.
        source.write_text(text, encoding="utf-8")
        page.wait_for_function("() => window.okfActivateGraphConcept('sub/one', 'keyboard')", timeout=15000)
        page.wait_for_selector('#detail-body[data-concept-id="sub/one"]', state="visible")
        expect(page.locator(".okf-composer__anchor code")).to_have_text("sub/one")
        expect(page.get_by_role("group", name="Draft target")).to_be_hidden()
        expect(status).to_have_count(0)
        expect(send).to_be_enabled()
        send.click()
        _posted(page)
        assert [(r["concept"], r["body"]) for r in _rows(page, base)] == [("sub/one", "One is back")]
        assert len(posts) == 2


def test_graph_draft_announces_each_other_selection_once(linked_bundle, page):
    two = "Your unsent comment stays on sub/one. It was not moved to sub/two."
    top = "Your unsent comment stays on sub/one. It was not moved to top."
    with live_server(linked_bundle) as base:
        _graph_ready(page, base)
        announced = _record_announcements(page)
        page.get_by_role("button", name="Comment on this concept", exact=True).click()
        box = page.get_by_role("textbox", name="Comment for agent")
        box.fill("Stays with one")

        _show_graph_concept(page, "sub/two")
        assert announced() == [two]
        _show_graph_concept(page, "top")
        assert announced() == [two, top]
        _show_graph_concept(page, "sub/two")  # already said for this selection
        assert announced() == [two, top]
        _show_graph_concept(page, "sub/one")  # the draft's own concept resets the record
        assert announced() == [two, top]
        _show_graph_concept(page, "sub/two")
        assert announced() == [two, top, two]

        page.get_by_role("button", name="Send", exact=True).click()
        _posted(page)
        assert [(r["concept"], r["body"]) for r in _rows(page, base)] == [("sub/one", "Stays with one")]


def test_loading_an_unchanged_document_does_not_replace_or_flash_its_enhanced_blocks(rich_bundle, page):
    """The live handshake re-fetches the open document on every load.

    Regression: that identical HTML was diffed against the enhanced DOM, so
    anchored headings, code and tables were replaced and pulsed as "changed" on
    every page load, and a selection made inside one of them was dropped.
    """
    page.add_init_script(
        "window.__patches = 0; window.addEventListener('okf-loom:bodyPatched', () => { window.__patches += 1; });")
    fetched = []
    page.on("response", lambda r: fetched.append(r.url) if "/__data/doc?" in r.url else None)
    with live_server(rich_bundle) as base:
        _concept_ready(page, f"{base}/rich")
        page.evaluate("() => { window.__code = document.querySelector('.okf-page__body pre'); }")
        _select_words(page, ".okf-page__body pre", "code gamma")
        expect(page.locator(".okf-comment-afford")).to_be_visible()
        for _ in range(100):  # the handshake's second fetch of the same revision
            if len(fetched) >= 2:
                break
            page.wait_for_timeout(50)
        assert len(fetched) >= 2, fetched
        page.wait_for_timeout(500)  # an absence can only be observed over time
        state = page.evaluate("""() => ({
          patches: window.__patches,
          sameCodeNode: window.__code === document.querySelector('.okf-page__body pre'),
          pulsing: document.querySelectorAll('.okf-page__body .okf-pulse').length,
          selection: getSelection().toString(),
        })""")
        assert (state["sameCodeNode"], state["pulsing"], state["selection"]) == (True, 0, "code gamma"), state
        assert state["patches"] <= 1, state
        expect(page.locator(".okf-comment-afford")).to_be_visible()


# ---- Portable feedback notes written offline in an export (X2) --------------

NOTE_KEYS = {"format", "version", "note_id", "target", "body", "quote", "claimed_author", "claimed_at"}


def _network_log(page):
    """Every request issued from now on; the note's own blob: download is local."""
    seen = []
    page.on("request", lambda r: None if r.url.startswith(("blob:", "data:")) else seen.append(r.url))
    return seen


def _open_composer(page, launcher):
    launcher.click()
    dialog = page.get_by_role("dialog", name="Write a feedback note")
    expect(dialog).to_be_visible()
    expect(page.locator("#okf-pfn-body")).to_be_focused()
    return dialog


def _download_note(page):
    with page.expect_download() as info:
        page.get_by_role("button", name="Download note").click()
    download = info.value
    data = Path(download.path()).read_bytes()
    return download.suggested_filename, data, portable_feedback.parse_note(data)


def _unload_guarded(page):
    return page.evaluate("""() => { const e = new Event('beforeunload', {cancelable: true});
      window.dispatchEvent(e); return e.defaultPrevented; }""")


def test_static_note_downloads_offline_bound_to_the_exact_disk_bytes(linked_bundle, tmp_path, page):
    two = linked_bundle / "sub" / "two.md"
    two.write_bytes(two.read_bytes().replace(b"\n", b"\r\n"))
    out = _export(linked_bundle, tmp_path, "static", None, include_portable_feedback=True)
    disk = (tmp_path / "export-src-static" / "sub" / "two.md").read_bytes()
    assert b"\r\n" in disk
    port = free_port()
    site = f"http://127.0.0.1:{port}"
    argv = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(out)]
    with managed_server(argv, site, tmp_path / "note-http.log"):
        for path in ("/index.html", "/sub/index.html", "/sub/log.html", "/__graph.html"):
            page.goto(site + path, wait_until="load")
            expect(page.locator("[data-okf-portable-feedback]")).to_have_count(0)

        page.goto(f"{site}/sub/two.html", wait_until="load")
        page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=site)
        requests = _network_log(page)
        launcher = page.locator("[data-okf-page-utilities]").get_by_role("button", name="Write feedback note")
        dialog = _open_composer(page, launcher)
        expect(dialog.locator(".okf-pfn__target")).to_have_text("Page: sub/two.md")
        expect(dialog.locator(".okf-pfn__disclosure")).to_contain_text("Nothing is sent anywhere")
        expect(dialog.locator(".okf-pfn__disclosure code")).to_have_text(hashlib.sha256(disk).hexdigest()[:12] + "\u2026")
        expect(page.locator("#okf-pfn-time")).not_to_be_checked()
        for field in ("#okf-pfn-quote", "#okf-pfn-name"):
            expect(page.locator(field)).to_have_value("")

        page.locator("#okf-pfn-body").fill("The totals row is out of date.")
        page.locator("#okf-pfn-quote").fill("More.")
        page.locator("#okf-pfn-name").fill("Ana")
        page.get_by_label("Include time").check()
        name, data, note = _download_note(page)
        assert (note.document, note.source_sha256) == ("sub/two.md", hashlib.sha256(disk).hexdigest())
        assert (note.body, note.quote, note.claimed_author) == ("The totals row is out of date.", "More.", "Ana")
        assert note.claimed_at and set(json.loads(data)) == NOTE_KEYS
        assert name == f"sub-two-{note.note_id[:8]}.okf-feedback.json"
        status = dialog.get_by_role("status")
        expect(status).to_contain_text(f"Downloaded {name}. Nothing was sent.")

        # The same content is the same note; any edit is a different note.
        again_name, again, same = _download_note(page)
        assert (again_name, again, same.note_id) == (name, data, note.note_id)
        page.locator("#okf-pfn-body").fill("The totals row is out of date since May.")
        expect(status).to_have_text("Changed since the last download. Download again to keep your changes.")
        assert _unload_guarded(page) is True
        _, _, edited = _download_note(page)
        assert edited.note_id != note.note_id and edited.body.endswith("since May.")
        assert _unload_guarded(page) is False

        # A copied note counts as kept, so closing needs no confirmation.
        page.get_by_label("Include time").uncheck()
        page.get_by_role("button", name="Copy JSON").click()
        expect(status).to_contain_text("Copied the note.")
        copied = portable_feedback.parse_note(page.evaluate("navigator.clipboard.readText()").encode("utf-8"))
        assert copied.claimed_at is None and copied.note_id not in (note.note_id, edited.note_id)
        page.get_by_role("button", name="Close").click()
        expect(dialog).to_be_hidden()
        expect(launcher).to_be_focused()
        expect(page.locator(".okf-pfn-launch__state")).to_be_hidden()
        assert requests == []
    assert page.failed_documents == []


def test_note_composer_keyboard_errors_and_unsaved_draft_guard(linked_bundle, tmp_path, page):
    out = _export(linked_bundle, tmp_path, "static", None, include_portable_feedback=True)
    page.goto((out / "sub" / "one.html").as_uri(), wait_until="load")
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    launcher = page.get_by_role("button", name="Write feedback note")
    body = page.locator("#okf-pfn-body")
    launcher.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Write a feedback note")
    expect(dialog).to_be_visible()
    expect(body).to_be_focused()

    # An empty note is refused inline, announced, and focus stays on the field.
    for _ in range(4):
        page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Download note")).to_be_focused()
    page.keyboard.press("Enter")
    expect(body).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#okf-pfn-body-error")).to_have_text("Write your note before downloading.")
    assert body.get_attribute("aria-describedby").split()[0] == "okf-pfn-body-error"
    expect(body).to_be_focused()
    assert downloads == [] and _unload_guarded(page) is False

    page.keyboard.type("Needs a diagram.")
    expect(body).not_to_have_attribute("aria-invalid", "true")
    expect(page.locator("#okf-pfn-body-error")).to_be_hidden()
    expect(page.locator("#okf-pfn-body-hint")).to_have_text("16 of 8,000 characters")
    assert _unload_guarded(page) is True

    # Escape asks before losing words; Keep editing returns to them.
    confirm = dialog.get_by_role("group", name="Your note hasn't been downloaded and will be lost if you leave.")
    page.keyboard.press("Escape")
    expect(confirm).to_be_visible()
    expect(page.get_by_role("button", name="Keep editing")).to_be_focused()
    page.keyboard.press("Enter")
    expect(confirm).to_be_hidden()
    expect(body).to_be_focused()
    expect(body).to_have_value("Needs a diagram.")

    # Discard is explicit, closes, returns focus and drops the guard.
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Keep editing")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Discard note")).to_be_focused()
    page.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    expect(launcher).to_be_focused()
    assert _unload_guarded(page) is False
    page.keyboard.press("Enter")
    expect(body).to_have_value("")

    # A draft closed by the browser itself is kept, flagged, and still guarded.
    page.keyboard.type("Keep me.")
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Keep editing")).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    state = page.locator(".okf-pfn-launch__state")
    expect(state).to_have_text("Draft not downloaded")
    expect(launcher).to_have_attribute("aria-describedby", state.get_attribute("id"))
    assert _unload_guarded(page) is True
    _open_composer(page, launcher)
    expect(body).to_have_value("Keep me.")
    expect(state).to_be_hidden()

    # Leaving with the draft raises the browser's own leave prompt.
    with page.expect_event("dialog") as prompt:
        page.close(run_before_unload=True)
    assert prompt.value.type == "beforeunload"
    prompt.value.dismiss()
    expect(body).to_have_value("Keep me.")
    assert downloads == []


def test_single_file_note_follows_the_concept_on_screen_from_disk(linked_bundle, tmp_path, page):
    out = _export(linked_bundle, tmp_path, "single-file", None, include_portable_feedback=True)
    source = tmp_path / "export-src-single-file"
    url = (out / "index.html").as_uri()
    _select(page, url + "?selected=sub/one")
    requests = _network_log(page)
    launcher = page.locator("#detail-content .okf-detail__header").get_by_role("button", name="Write feedback note")
    dialog = _open_composer(page, launcher)
    expect(dialog.locator(".okf-pfn__target")).to_have_text("Page: sub/one.md")
    page.locator("#okf-pfn-body").fill("Link the log from here.")
    name, _, note = _download_note(page)
    assert (note.document, note.source_sha256) == (
        "sub/one.md", hashlib.sha256((source / "sub" / "one.md").read_bytes()).hexdigest())
    assert name == f"sub-one-{note.note_id[:8]}.okf-feedback.json"
    page.get_by_role("button", name="Close").click()
    expect(launcher).to_be_focused()
    assert requests == []

    # Another concept is another note with its own empty draft.
    _link(page, "sibling-link").click()
    page.wait_for_selector('#detail-body[data-concept-id="sub/two"]')
    dialog = _open_composer(page, launcher)
    expect(dialog.locator(".okf-pfn__target")).to_have_text("Page: sub/two.md")
    expect(page.locator("#okf-pfn-body")).to_have_value("")
    page.locator("#okf-pfn-body").fill("Two reads well.")
    _, _, other = _download_note(page)
    assert other.document == "sub/two.md" and other.note_id != note.note_id
    page.get_by_role("button", name="Close").click()
    assert requests == []

    # A reserved document and the concept list are not note targets.
    _select(page, url + "?selected=sub/one")
    _link(page, "dir-log-link").click()
    page.locator("#detail-document").wait_for(state="visible")
    expect(page.locator("[data-okf-portable-feedback]")).to_be_hidden()
    page.get_by_role("button", name="Back to One").press("Enter")
    expect(launcher).to_be_visible()
    page.goto(url + "#okf-single-index", wait_until="load")
    page.wait_for_function("() => document.body.dataset.okfSingleDestination === 'index'")
    expect(page.locator("[data-okf-portable-feedback]")).to_be_hidden()


def test_note_copy_falls_back_to_manual_selection_on_a_phone(linked_bundle, tmp_path, page):
    out = _export(linked_bundle, tmp_path, "static", None, include_portable_feedback=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true})")
    page.goto((out / "sub" / "one.html").as_uri(), wait_until="load")
    launcher = page.get_by_role("button", name="Write feedback note")
    launcher.scroll_into_view_if_needed()
    dialog = _open_composer(page, launcher)
    page.locator("#okf-pfn-body").fill("Readable on a phone.")
    page.get_by_role("button", name="Copy JSON").click()
    manual = page.locator("#okf-pfn-manual-text")
    expect(manual).to_be_visible()
    expect(manual).to_be_focused()
    assert manual.evaluate("t => t.selectionStart === 0 && t.selectionEnd === t.value.length")
    note = portable_feedback.parse_note(manual.input_value().encode("utf-8"))
    assert note.document == "sub/one.md" and note.body == "Readable on a phone."
    expect(dialog.get_by_role("status")).to_contain_text(f"save it as sub-one-{note.note_id[:8]}.okf-feedback.json")

    layout = dialog.evaluate("""d => ({
      left: d.getBoundingClientRect().left, right: d.getBoundingClientRect().right,
      overflow: d.scrollWidth - d.clientWidth, page: document.documentElement.scrollWidth,
      short: [...d.querySelectorAll('.okf-btn')].filter(b => b.getClientRects().length && b.getBoundingClientRect().height < 44).length })""")
    assert layout["left"] >= 0 and layout["right"] <= 390 and layout["overflow"] <= 0, layout
    assert layout["page"] <= 390 and layout["short"] == 0, layout

    # Showing the text is not keeping it: closing still asks.
    page.locator("#okf-pfn-body").press("End")
    page.keyboard.type(" Really.")
    expect(manual).to_be_hidden()
    page.get_by_role("button", name="Close").click()
    expect(page.get_by_role("button", name="Keep editing")).to_be_focused()
    page.get_by_role("button", name="Discard note").click()
    expect(dialog).to_be_hidden()
