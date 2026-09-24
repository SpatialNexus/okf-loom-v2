"""F11/F12 focused browser proof: honest page-feedback labels and an
acknowledgement-aware Watching status toggle.

Every journey drives a real ``okf serve`` process and the shipped studio
client. Routing injection is used ONLY at the HTTP seam for failure classes a
healthy local server cannot produce on demand (503, lost transport, withheld
responses); positive paths and the real cross-origin denial stay untouched.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

from browser_support import expect, launch_chromium, live_server, sync_playwright

from conftest import okf_module_argv, okf_subprocess_env

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.browser


@pytest.fixture
def studio_bundle(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.md").write_text("# Home\n\nRoot overview.\n", encoding="utf-8")
    (root / "note.md").write_text(
        "---\ntype: Note\ntitle: Note\n---\n\n# Note\n\nIntro paragraph.\n\n## Details\n\nMore text.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        yield browser
        browser.close()


# ---- shared helpers -----------------------------------------------------------


def _boot(page, base, path="/note"):
    page.goto(base + path, wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'")
    settled = page.wait_for_function(
        "() => { const s = window.okfLoomStudio.state.commentsStatus;"
        " return ['ready', 'error'].includes(s) ? s : false; }").json_value()
    assert settled == "ready"


def _open_studio_home(page):
    launcher = page.locator(".okf-studio-launcher")
    expect(launcher).to_be_visible()
    launcher.click()
    expect(page.locator("#okf-panel-title")).to_have_text("Studio")


def _watching_settled(page, pressed: bool, timeout: int = 5000) -> None:
    """Ack-synchronised settlement of the Watching control: aria-pressed, the
    accessible name and the busy marker all agree on one reported state."""
    page.wait_for_function(
        """(pressed) => { const t = document.querySelector('.okf-watch-toggle');
            return t.getAttribute('aria-pressed') === pressed
                && t.getAttribute('aria-busy') !== 'true'
                && t.getAttribute('aria-label') === 'Agent watching, currently '
                    + (pressed === 'true' ? 'on' : 'off'); }""",
        arg="true" if pressed else "false", timeout=timeout,
    )


def _okf(bundle, *argv):
    done = subprocess.run(
        [str(ROOT / "scripts" / "okf-loom"), *argv],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, (argv, done.stdout, done.stderr)
    return done.stdout


def _rows(page, base):
    data = page.request.get(f"{base}/__comments").json()
    return data["comments"] if isinstance(data, dict) else data


def _row(page, base, body):
    return next(r for r in _rows(page, base) if r["body"] == body)


def _card(page, body):
    card = page.locator(".okf-comment", has_text=body).filter(
        has_not=page.locator(".okf-comment__posting")).first
    expect(card).to_be_visible()
    return card


def _comment(page, body):
    page.get_by_role("button", name="Comment on this page", exact=True).click()
    box = page.get_by_role("textbox", name="Comment for agent")
    expect(box).to_be_focused()
    box.fill(body)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator("#okf-annotation-status")).to_have_text("Comment posted.")
    _card(page, body)


def _reply(page, parent_body, body):
    _card(page, parent_body).get_by_role("button", name="Reply", exact=True).first.click()
    box = page.get_by_role("textbox", name="Reply to comment")
    box.fill(body)
    box.press("Control+Enter")
    saved = page.locator(".okf-comment--reply", has_text=body).filter(
        has_not=page.locator(".okf-comment__posting")).first
    expect(saved).to_be_visible()
    expect(page.get_by_role("textbox", name="Reply to comment")).to_have_count(0)


def _active_badge(page, *, text, label, present):
    """Checkpoint the active-comment chip where it really lives: the Studio
    home panel's Comments control. The panel is closed again afterwards so the
    page-feedback group stays the asserted surface."""
    page.locator(".okf-studio-launcher").click()
    expect(page.locator("#okf-panel-title")).to_have_text("Studio")
    badge = page.locator(".okf-badge").first
    if present:
        expect(badge).to_be_visible()
        expect(badge).to_have_attribute("aria-label", label)
        assert badge.text_content() == text, badge.text_content()
    else:
        expect(badge).to_be_hidden()
    page.get_by_role("button", name="Close panel").click()
    expect(page.locator(".okf-studio-launcher")).to_be_visible()


def _post_collector(page):
    posts: list = []
    page.on("request", lambda req: posts.append(req)
            if "/__presence" in req.url and req.method == "POST" else None)
    return posts


# ---- F11: page-feedback labels follow real lifecycle states --------------------


def test_page_feedback_labels_track_real_comment_states(studio_bundle, browser):
    """dismissed-only / mixed / resolved-only labels vs the canonical stored
    rows. A dismissed row is never announced as resolved; resolved-only keeps
    its explicit wording; mixed terminal states use a neutral total."""
    with live_server(studio_bundle) as base:
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _boot(page, base)
        group = page.get_by_role("group", name="Page feedback")

        _comment(page, "Please fix the intro")
        expect(group.get_by_role("button", name="1 open comment on this page", exact=True)).to_be_visible()
        assert _row(page, base, "Please fix the intro")["state"] == "open"

        # dismissed-only: the real Cancel control, then the canonical row.
        _card(page, "Please fix the intro").get_by_role("button", name="Cancel", exact=True).click()
        expect(group.get_by_role("button", name="1 dismissed comment on this page", exact=True)).to_be_visible()
        assert _row(page, base, "Please fix the intro")["state"] == "dismissed"
        # Active-only chips: a dismissed row is not active.
        _active_badge(page, text="0", label="0 open comments", present=False)

        # mixed terminal (one dismissed + one resolved): neutral total.
        _comment(page, "Please also fix the outro")
        row_b = _row(page, base, "Please also fix the outro")
        _okf(studio_bundle, "comment-resolve", str(studio_bundle), row_b["id"], "--summary", "done")
        expect(group.get_by_role("button", name="View 2 comments on this page", exact=True)).to_be_visible()

        # resolved-only clarity is retained once nothing is dismissed.
        row_a = _row(page, base, "Please fix the intro")
        _okf(studio_bundle, "comment-resolve", str(studio_bundle), row_a["id"], "--summary", "done too")
        expect(group.get_by_role("button", name="2 resolved comments on this page", exact=True)).to_be_visible()
        states = sorted(r["state"] for r in _rows(page, base))
        assert states == ["resolved", "resolved"], states
        # Resolved rows never count as active either.
        _active_badge(page, text="0", label="0 open comments", present=False)
        assert not errors, errors
        page.close()


def test_page_feedback_counts_active_replies_and_respects_archived_roots(studio_bundle, browser):
    """Active replies keep the active-only counting semantics of the index and
    graph chips; an archived root drops out of the page count without changing
    any lifecycle rule (its non-archived resolved reply still counts)."""
    with live_server(studio_bundle) as base:
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _boot(page, base)
        group = page.get_by_role("group", name="Page feedback")

        _comment(page, "Root ask")
        _reply(page, "Root ask", "A person answer")
        # Both rows are active on this page, exactly as the chips count them.
        expect(group.get_by_role("button", name="2 open comments on this page", exact=True)).to_be_visible()
        _active_badge(page, text="2", label="2 open comments", present=True)

        root_row = _row(page, base, "Root ask")
        reply_row = _row(page, base, "A person answer")
        # Active-reply fixture: resolve the root, keep the reply open.
        _okf(studio_bundle, "comment-resolve", str(studio_bundle), root_row["id"], "--summary", "done")
        expect(group.get_by_role("button", name="1 open comment on this page", exact=True)).to_be_visible()
        _active_badge(page, text="1", label="1 open comments", present=True)
        stored = {r["body"]: r for r in _rows(page, base)}
        assert stored["Root ask"]["state"] == "resolved"
        assert stored["A person answer"]["state"] == "open"

        # Resolve the reply, then archive the thread through its real control.
        _okf(studio_bundle, "comment-resolve", str(studio_bundle), reply_row["id"], "--summary", "answered")
        expect(group.get_by_role("button", name="2 resolved comments on this page", exact=True)).to_be_visible()
        # Reopen the Comments panel through the page count itself.
        group.get_by_role("button", name="2 resolved comments on this page", exact=True).click()
        _card(page, "Root ask").get_by_role("button", name="Archive thread", exact=True).click()
        expect(group.get_by_role("button", name="1 resolved comment on this page", exact=True)).to_be_visible()
        stored = {r["body"]: r for r in _rows(page, base)}
        assert stored["Root ask"]["archived"] is True
        assert stored["A person answer"]["archived"] is False
        assert stored["A person answer"]["state"] == "resolved"
        # Active-only chip semantics: nothing here is active any more.
        _active_badge(page, text="0", label="0 open comments", present=False)
        assert not errors, errors
        page.close()


# ---- F12: acknowledgement-aware Watching status toggle --------------------------


def test_watching_success_adopts_canonical_state_and_reaches_a_second_viewer(studio_bundle, browser):
    """Real successful POST: the control settles on the acknowledged canonical
    reported state (status flag only, one write per click), and a second local
    viewer's canonical presence render adopts the same agent row."""
    with live_server(studio_bundle) as base:
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        posts = _post_collector(page)
        _boot(page, base)
        _open_studio_home(page)
        toggle = page.locator(".okf-watch-toggle")
        expect(toggle).to_be_visible()
        assert "does not start an agent" in (toggle.get_attribute("title") or "")

        toggle.click()
        _watching_settled(page, True)
        assert len(posts) == 1, [p.post_data for p in posts]
        assert '"agent"' in posts[0].post_data and '"watching"' in posts[0].post_data

        # Two-viewer canonical presence journey: the second viewer never
        # writes; its canonical render owns both the chip and its own switch.
        viewer = browser.new_context().new_page()
        viewer_errors: list[str] = []
        viewer.on("pageerror", lambda e: viewer_errors.append(str(e)))
        _boot(viewer, base)
        _open_studio_home(viewer)
        expect(viewer.locator(".okf-presence")).to_have_attribute(
            "aria-label", re.compile("Agent presence: watching"), timeout=15000)
        _watching_settled(viewer, True)
        assert not viewer_errors, viewer_errors
        # The reporting viewer is equally canonical: the chip names the state.
        expect(page.locator(".okf-presence")).to_have_attribute(
            "aria-label", re.compile("Agent presence: watching"))
        # R3: the hover-only caveat is also the chip's description, so it
        # reaches keyboard, touch and screen-reader users.
        expect(page.locator(".okf-presence")).to_have_accessible_description(
            "Reported status, not proof an agent is running.")
        # Hidden, so browse mode does not read it again after the chip.
        expect(page.locator("#okf-presence-note")).to_be_hidden()
        assert not errors, errors
        viewer.close()
        page.close()


def test_watching_toggle_denials_roll_back_and_are_announced(studio_bundle, browser):
    """Definite rejections (real 403 cross-origin denial, routed 403/503, and a
    parsed ok:false envelope) roll the optimistic flip back and say so; an
    invalid acknowledgement on 200 stays honest uncertainty instead."""
    with live_server(studio_bundle) as base:
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _boot(page, base)
        _open_studio_home(page)
        toggle = page.locator(".okf-watch-toggle")

        # Real server denial evidence: the legacy route refuses a bad token.
        refused = page.evaluate(
            """async () => {
                const r = await fetch('/__presence', { method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-OKF-Token': 'not-the-token' },
                    body: JSON.stringify({ actor: 'agent', state: 'watching' }) });
                return r.status;
            }"""
        )
        assert refused == 403, refused
        assert toggle.get_attribute("aria-pressed") == "false"

        def _denial_route(status, body):
            def handler(route):
                if route.request.method != "POST":
                    route.continue_()
                    return
                route.fulfill(status=status, content_type="application/json", body=body)
            return handler

        posts = _post_collector(page)
        page.route("**/__presence", _denial_route(403, '{"ok":false,"error":"forbidden"}'))
        toggle.click()
        _watching_settled(page, False)
        expect(page.locator(".okf-toast__msg", has_text="Could not update agent watching state.").first).to_be_visible()
        assert len(posts) == 1 and '"watching"' in posts[0].post_data, posts

        page.unroute("**/__presence")
        page.route("**/__presence", _denial_route(503, '{"ok":false,"error":"unavailable"}'))
        toggle.click()
        _watching_settled(page, False)
        expect(page.locator(".okf-toast__msg", has_text="Could not update agent watching state.").nth(1)).to_be_visible()
        assert len(posts) == 2 and '"watching"' in posts[1].post_data, posts

        page.unroute("**/__presence")
        page.route("**/__presence", _denial_route(200, '{"ok":false,"error":"denied"}'))
        toggle.click()
        _watching_settled(page, False)
        expect(page.locator(".okf-toast__msg", has_text="Could not update agent watching state.").nth(2)).to_be_visible()
        assert len(posts) == 3, posts

        # Invalid acknowledgement (200, unusable body): rolled back AND honest
        # that nothing was confirmed — never claimed as a rejection. The three
        # earlier rejection toasts are still counting down, so assert on the
        # toast that appears, not on the whole region.
        page.unroute("**/__presence")
        page.route("**/__presence", _denial_route(200, '<html>gateway noise</html>'))
        toggle.click()
        _watching_settled(page, False)
        expect(page.locator(".okf-toast__msg", has_text="could not be confirmed").first).to_be_visible()
        assert len(posts) == 4, posts
        # No routed fulfilment ever touched the server: canonical presence
        # still has no watching agent row.
        stored = page.request.get(f"{base}/__presence").json()
        agent = next((r for r in stored["records"] if r.get("kind") == "agent"), None)
        assert agent is None or agent.get("state") != "watching", stored
        assert not errors, errors
        page.close()


def test_watching_transport_loss_is_uncertainty_never_rejection(studio_bundle, browser):
    """A lost request before acknowledgement, and a lost response after a real
    accepted write, both stay honest: no 'could not update' rejection claim,
    and canonical presence reconciliation owns the final state."""
    with live_server(studio_bundle) as base:
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _boot(page, base)
        _open_studio_home(page)
        toggle = page.locator(".okf-watch-toggle")
        posts = _post_collector(page)
        # Observe before either loss: the region is created lazily, and later
        # toasts are appended inside it rather than directly to document.body.
        page.evaluate(
            """() => {
                window.__toastLog = [];
                new MutationObserver((mutations) => {
                    for (const mutation of mutations) {
                        for (const node of mutation.addedNodes) {
                            if (node.nodeType === Node.ELEMENT_NODE && node.matches('.okf-toast')) {
                                window.__toastLog.push(node.textContent || '');
                            }
                        }
                    }
                }).observe(document.body, {childList: true, subtree: true});
            }""")

        # Loss before acknowledgement: the request never leaves the browser.
        # Deterministic: no write lands, so no delta can pre-empt the failure.
        page.route("**/__presence", lambda route: (
            route.continue_() if route.request.method != "POST" else route.abort("connectionreset")))
        toggle.click()
        _watching_settled(page, False)
        expect(page.locator(".okf-toast__msg", has_text="could not be confirmed")).to_be_visible()
        page.wait_for_function(
            "() => window.__toastLog.some((text) => text.includes('could not be confirmed'))")
        assert page.locator(".okf-toast__msg", has_text="Could not update agent watching state.").count() == 0
        assert len(posts) == 1, posts

        # Loss with a real accepted write: route.fetch() lets the real request
        # reach the real server (the write lands), then the response is dropped.
        # The SSE delta and the transport failure race each other, so record
        # every toast as it appears and assert on the WHOLE log afterwards:
        # whichever order wins, no rejection may ever be claimed.
        page.unroute("**/__presence")
        # The first leg positively proved this observer; isolate the second
        # leg's log without replacing the live observer.
        page.evaluate("() => { window.__toastLog = []; }")

        def drop_response_after_real_write(route):
            if route.request.method != "POST":
                route.continue_()
                return
            try:
                route.fetch()
            except Exception:
                pass
            route.abort("connectionreset")
        page.route("**/__presence", drop_response_after_real_write)
        toggle.click()
        # The write provably landed: the server's own delta reconciles the
        # control to the canonical watching state without another write.
        expect(page.locator(".okf-presence")).to_have_attribute(
            "aria-label", re.compile("Agent presence: watching"), timeout=15000)
        _watching_settled(page, True)
        assert len(posts) == 2, posts
        toasts = page.evaluate("() => window.__toastLog")
        assert not any("Could not update agent watching state" in t for t in toasts), toasts
        stored = page.request.get(f"{base}/__presence").json()
        agent = next(r for r in stored["records"] if r.get("kind") == "agent")
        assert agent["state"] == "watching", stored
        assert not errors, errors
        page.close()


def test_watching_late_response_cannot_roll_back_newer_canonical_presence(studio_bundle, browser):
    """A newer canonical presence update that arrives while an older write is
    still pending wins: delivering the late (real, accepted) acknowledgement
    afterwards must neither roll the control back nor relabel the newer state."""
    with live_server(studio_bundle) as base:
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        posts = _post_collector(page)
        delivered: list = []
        page.on("response", lambda resp: delivered.append(resp)
                if "/__presence" in resp.url and resp.request.method == "POST" else None)
        _boot(page, base)
        _open_studio_home(page)
        toggle = page.locator(".okf-watch-toggle")

        held: dict = {}
        def withhold_real_write(route):
            if route.request.method != "POST":
                route.continue_()
                return
            held["response"] = route.fetch()  # the real write lands immediately
            held["route"] = route             # its delivery stays withheld
        page.route("**/__presence", withhold_real_write)

        toggle.click()
        page.wait_for_function(
            "() => document.querySelector('.okf-watch-toggle').getAttribute('aria-busy') === 'true'",
            timeout=5000,
        )
        # A newer canonical update arrives from the real cross-process CLI
        # owner while the click's response is withheld.
        _okf(studio_bundle, "presence", str(studio_bundle), "--state", "editing")
        expect(page.locator(".okf-presence")).to_have_attribute(
            "aria-label", re.compile("Agent presence: editing"), timeout=15000)
        page.wait_for_function(
            """() => { const t = document.querySelector('.okf-watch-toggle');
                return t.getAttribute('aria-pressed') === 'false'
                    && t.getAttribute('aria-label') === 'Agent watching, currently off'; }""",
            timeout=5000,
        )

        # Release the late 200 watching acknowledgement and observe the real
        # response delivery and the control's continuation BEFORE asserting.
        held["route"].fulfill(response=held["response"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not delivered:
            page.wait_for_timeout(50)
        assert delivered and delivered[0].status == 200, [r.status for r in delivered]
        page.wait_for_function(
            "() => document.querySelector('.okf-watch-toggle').getAttribute('aria-busy') !== 'true'",
            timeout=5000,
        )

        # Final assertions: the stale watching ack owns nothing.
        page.wait_for_function(
            """() => { const t = document.querySelector('.okf-watch-toggle');
                return t.getAttribute('aria-pressed') === 'false'
                    && t.getAttribute('aria-label') === 'Agent watching, currently off'; }""",
            timeout=5000,
        )
        expect(page.locator(".okf-presence")).to_have_attribute(
            "aria-label", re.compile("Agent presence: editing"))
        assert len(posts) == 1, [p.post_data for p in posts]  # no duplicate write
        assert page.locator(".okf-toast").count() == 0
        assert not errors, errors
        page.close()
