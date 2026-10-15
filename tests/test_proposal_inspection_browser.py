"""X3: the opt-in, local, inspection-only Studio Proposals panel.

Browser proof against a real ``serve --enable-proposal-review`` process:
proposals are created through the real HTTP proposal API (the local-agent
path) and the panel only ever reads them with GET. It never POSTs, never
sees the private proposal token and offers no decision controls.
"""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

import pytest

from conftest import okf_module_argv, okf_subprocess_env, private_session_root
from okf_loom.proposal_review.activation import ProductionCapturePort

from browser_support import copy_bundle, expect, launch_chromium, live_server, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "samples" / "demo_bundle"
MUTATION_NAMES = re.compile(r"\b(accept|reject|apply|withdraw|undo|decide)\b", re.I)
LABELS_NOTE = "Author and decision-actor names are unverified labels, not authenticated identities."
NOT_REPORTED = "Apply/undo history is not reported by this view."
DETAIL_ROUTE = re.compile(r"/__proposal/v1/proposals/[^/?]+/revisions/\d+$")
LIST_ROUTE = re.compile(r"/__proposal/v1/proposals\?")


def _session_tokens(bundle: Path) -> tuple[str, str]:
    session = bundle / ".okf-loom" / "session"
    return ((session / ".token").read_text().strip(),
            (session / ".proposal-http-token").read_text().strip())


def _create_proposal(base: str, bundle: Path, *, target: str, summary: str,
                     op_id: str, key: str, tags: list[str],
                     rationale: str = "Keep table tags aligned with the catalogue.") -> dict:
    """Create one proposal the way a local agent does: HTTP + private token."""
    csrf, proposal_token = _session_tokens(bundle)
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    snapshot = ProductionCapturePort(bundle).capture(
        captured_at=captured_at, proposal_capability_active=True).snapshot
    revisions = {entry.to_mapping()["concept"]: entry.revision for entry in snapshot.concepts}
    body = {
        "schema": 1, "mode": "proposal", "author": "agent", "summary": summary,
        "rationale": rationale,
        "expected_snapshot": snapshot.to_mapping(),
        "operations": [{
            "schema": 1, "op_id": op_id, "kind": "set_frontmatter", "target": target,
            "args": {"key": "tags", "value": tags}, "reads": [target], "writes": [target],
            "expected_revisions": {target: revisions[target]}, "depends_on": [], "extensions": {},
        }],
        "provenance": [], "extensions": {},
    }
    payload = json.dumps(body).encode()
    netloc = urlsplit(base).netloc
    host, port = netloc.split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=30)
    try:
        conn.request("POST", "/__proposal/v1/proposals", body=payload, headers={
            "Host": netloc, "X-OKF-Token": csrf, "X-OKF-Proposal-Token": proposal_token,
            "Content-Type": "application/json", "Content-Length": str(len(payload)),
            "Idempotency-Key": key,
        })
        response = conn.getresponse()
        raw = response.read()
    finally:
        conn.close()
    assert response.status == 201, raw[:400]
    return json.loads(raw)["data"]


def _cli(*argv: str) -> dict:
    """Run one `okf-loom proposal ...` command the way a local operator does."""
    result = subprocess.run(okf_module_argv(*argv, "--format", "json"), cwd=ROOT,
                            env=okf_subprocess_env(), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (argv[:2], result.stdout[-800:], result.stderr[-800:])
    return json.loads(result.stdout)["data"]


def _wait_for_get(page, requests, start: int, route: re.Pattern, timeout: float = 20.0) -> list:
    """Wait for a proposal GET matching ``route`` issued after ``requests[start]``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hits = [r for r in requests[start:] if r.method == "GET" and route.search(r.url)]
        if hits:
            return hits
        page.wait_for_timeout(100)
    raise AssertionError(f"no fresh GET of {route.pattern} after request #{start}")


def _page_transition(page, kind: str) -> None:
    """Dispatch a persisted pagehide/pageshow through the real listeners.

    Headless Playwright Chromium does not put these no-store, live-streaming
    Studio pages into the real back/forward cache, so the BFCache freeze and
    restore are driven as the exact PageTransitionEvents the browser sends.
    """
    page.evaluate("kind => window.dispatchEvent(new PageTransitionEvent(kind, {persisted: true}))", kind)


def _leaked(page, markers: list[str]) -> list[str]:
    html = page.evaluate("() => document.documentElement.outerHTML")
    return [m for m in markers if m in html]


def _assert_released(page) -> None:
    retained = page.evaluate("() => window.okfLoomStudio._proposalRetention()")
    assert retained == {"root": False, "shell": False, "live": False, "rootText": 0, "seed": False,
                        "detail": False, "items": 0, "byteHolders": 0}, retained


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        owned = launch_chromium(playwright)
        try:
            yield owned
        finally:
            owned.close()


# Track every EventSource live.js opens so a test can fail the current stream
# exactly the way the browser reports a fatal stream failure (readyState
# CLOSED, then an ``error`` event). Recovery then runs through the real
# live.js reconnect path and a new real SSE connection to the server.
TRACK_EVENT_SOURCES = """(() => {
  const Native = window.EventSource;
  if (!Native) return;
  window.__x3Sources = [];
  window.EventSource = class extends Native {
    constructor(...args) { super(...args); window.__x3Sources.push(this); }
  };
})();"""


def _page(browser, *, width=1440, height=900, page_size=None, track_sources=False):
    context = browser.new_context(viewport={"width": width, "height": height})
    if track_sources:
        context.add_init_script(TRACK_EVENT_SOURCES)
    if page_size is not None:
        context.add_init_script(
            f"window.__OKF_LOOM_TEST__ = true; window.__OKF_LOOM_PROPOSAL_PAGE_SIZE__ = {int(page_size)};")
    page = context.new_page()
    requests: list = []
    page.on("request", lambda request: requests.append(request))
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return context, page, requests, errors


def _proposal_requests(requests) -> list:
    return [r for r in requests if "/__proposal/" in r.url]


def _open_studio(page, base: str, *, ready: str = "window.okfLoomStudio.state.commentsReady") -> None:
    page.goto(base + "/tables/orders", wait_until="load")
    page.wait_for_function(f"() => window.okfLoomStudio && {ready}")
    page.get_by_role("button", name="Studio", exact=False).click()
    expect(page.get_by_role("heading", name="Studio", exact=True)).to_be_visible()


def _assert_read_only_traffic(requests, proposal_token: str) -> None:
    traffic = _proposal_requests(requests)
    assert traffic, "the panel never read the proposal routes"
    for request in traffic:
        assert request.method == "GET", (request.method, request.url)
        headers = {k.lower(): v for k, v in request.headers.items()}
        assert "x-okf-proposal-token" not in headers
        assert proposal_token not in request.url
        assert re.search(r"/__proposal/v1/proposals(\?|/[^/?]+/revisions/\d+$)", request.url), request.url


@pytest.mark.browser
def test_inactive_capability_offers_no_proposals_entry_and_never_requests(tmp_path: Path, browser) -> None:
    bundle = copy_bundle(DEMO, tmp_path / "bundle")
    private_session_root(bundle)
    with live_server(bundle) as base:
        context, page, requests, errors = _page(browser)
        try:
            _open_studio(page, base)
            expect(page.locator("[data-proposal-state='unavailable']")).to_contain_text("Experimental")
            expect(page.get_by_role("button", name="Inspect proposals")).to_have_count(0)
            expect(page.get_by_role("button", name="Proposals", exact=True)).to_have_count(0)
            assert page.evaluate("() => window.okfLoomStudio.state.openPanel") != "proposals"
            page.wait_for_timeout(300)
            assert _proposal_requests(requests) == []
            assert errors == []
        finally:
            context.close()


@pytest.mark.browser
def test_local_read_only_page_explains_cli_path_without_requests(tmp_path: Path, browser) -> None:
    bundle = copy_bundle(DEMO, tmp_path / "bundle")
    private_session_root(bundle)
    with live_server(bundle, "--enable-proposal-review", "--no-edit") as base:
        context, page, requests, errors = _page(browser)
        try:
            _open_studio(page, base, ready="true")
            # Read-only pages keep the Studio home entry (no composer entry).
            expect(page.get_by_role("button", name="Inspect proposals")).to_have_count(0)
            page.get_by_role("button", name="Proposals", exact=True).click()
            blocked = page.get_by_role("heading", name="Proposals can’t be inspected from this read-only page")
            expect(blocked).to_be_visible()
            expect(page.get_by_label("Proposal inspection commands")).to_contain_text("scripts/okf-loom proposal list")
            box = page.locator("#okf-panel").bounding_box()
            assert box and box["width"] <= 441, box  # no diff to lay out: normal drawer width
            page.wait_for_timeout(300)
            assert _proposal_requests(requests) == []
            assert errors == []
        finally:
            context.close()


@pytest.mark.browser
def test_list_detail_diff_conflict_and_layouts_are_inspection_only(tmp_path: Path, browser) -> None:
    bundle = copy_bundle(DEMO, tmp_path / "bundle")
    private_session_root(bundle)
    with live_server(bundle, "--enable-proposal-review") as base:
        _create_proposal(base, bundle, target="tables/orders", summary="Tag orders",
                         op_id="op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", key="A" * 32, tags=["orders"])
        _, proposal_token = _session_tokens(bundle)
        context, page, requests, errors = _page(browser)
        try:
            # The first list read fails once: the error is stated and Retry recovers.
            failed = {"done": False}

            def flaky(route):
                if failed["done"]:
                    route.continue_()
                    return
                failed["done"] = True
                route.fulfill(status=503, content_type="application/json", body=json.dumps({
                    "schema": 1, "error": {"code": "service_unavailable", "message": "Busy.",
                                           "request_id": "req_test", "retryable": True,
                                           "resync_required": False, "details": {}}}))
            page.route(re.compile(r"/__proposal/v1/proposals\?"), flaky)

            _open_studio(page, base)
            assert proposal_token not in page.content()
            page.get_by_role("button", name="Inspect proposals").click()
            panel = page.locator("#okf-panel")
            expect(panel).to_have_attribute("data-okf-panel", "proposals")
            expect(panel.get_by_text("Proposals could not be loaded.", exact=True)).to_be_visible()
            expect(panel.get_by_text("The proposal service is busy. Try again in a moment.", exact=True)).to_be_visible()
            expect(panel.get_by_text("Code service_unavailable · request req_test", exact=True)).to_be_visible()
            panel.get_by_role("button", name="Retry").click()

            heading = page.locator("#okf-proposals-list-title")
            expect(heading).to_have_text("All proposals")
            expect(heading).to_be_focused()
            # R3: Proposals keeps its own intro, which now separates a proposal
            # from a comment and a saved change; it gets no panel purpose line.
            expect(panel.locator(".okf-proposals__intro")).to_contain_text(
                "A proposal is neither a comment nor a saved change yet.")
            expect(page.locator("#okf-panel-purpose")).to_be_hidden()
            row = panel.locator(".okf-proposal-row__open")
            expect(row).to_have_count(1)
            expect(row).to_contain_text("Tag orders")
            expect(row).to_contain_text("Revision 1 · by agent (unverified) · created")
            caption = panel.locator(".okf-proposals__caption")
            expect(caption).to_contain_text("Operation counts, decisions and validation are not reported in the list")
            expect(caption).to_contain_text(LABELS_NOTE)
            box = panel.bounding_box()
            assert box and box["width"] > 900, box  # wide inspection panel beside/over the page

            row.click()
            title = page.locator("#okf-proposal-detail-title")
            expect(title).to_have_text("Tag orders")
            expect(title).to_be_focused()
            for name in ("Rationale", "Conflicts · 0", "Changes · 1 concept", "Operations · 1",
                         "Validation", "Retention and apply/undo"):
                expect(panel.get_by_role("heading", name=name, exact=True)).to_be_visible()
            # Labels are unverified; unreported history/retention is never "none".
            expect(panel.locator(".okf-proposals__intro")).to_contain_text(LABELS_NOTE)
            expect(panel.locator(".okf-proposal-facts dt", has_text="Author (unverified)")).to_have_count(1)
            lifecycle = panel.locator("section[aria-labelledby='okf-proposal-sec-lifecycle']")
            expect(lifecycle.get_by_text(NOT_REPORTED, exact=True)).to_be_visible()
            for label in ("Replay record kept until", "Undo backups kept until", "Audit record kept until"):
                expect(lifecycle.locator("dt", has_text=label).locator("xpath=following-sibling::dd[1]")).to_have_text("Not reported")
            expect(lifecycle).not_to_contain_text("No apply or undo attempts")
            expect(lifecycle).not_to_contain_text("Undo available")
            expect(lifecycle).not_to_contain_text("not scheduled")
            expect(panel.get_by_text("Keep table tags aligned with the catalogue.", exact=True)).to_be_visible()
            expect(panel.get_by_text("No conflicts with the current bundle.", exact=True)).to_be_visible()

            diff = panel.get_by_role("region", name="Diff of tables/orders")
            expect(diff).to_be_visible()
            expect(diff.locator("table.okf-proposal-diff--split")).to_have_count(1)
            expect(diff.locator(".okf-proposal-diff__cell--del").filter(has_text="revenue")).not_to_have_count(0)
            expect(diff.locator(".okf-proposal-diff__cell--add").first).to_be_visible()
            layout = panel.get_by_role("group", name="Diff layout")
            expect(layout.get_by_role("button", name="Side by side")).to_have_attribute("aria-pressed", "true")
            unified = layout.get_by_role("button", name="Unified")
            unified.click()
            expect(unified).to_have_attribute("aria-pressed", "true")
            expect(unified).to_be_focused()
            expect(diff.locator("table.okf-proposal-diff--unified")).to_have_count(1)
            expect(diff.locator(".okf-proposal-diff__row--del").filter(has_text="revenue")).not_to_have_count(0)

            panel.get_by_text("Full proposed-after text", exact=True).click()
            after = panel.locator("pre[aria-label='Full proposed-after text']")
            expect(after).to_be_visible()
            expect(after).to_contain_text("title: Orders")

            # Inspection only: nothing in the panel can decide, apply or withdraw.
            names = panel.locator("button, a, summary, [role=button]").evaluate_all(
                "els => els.map(e => (e.getAttribute('aria-label') || e.textContent || '').trim())")
            assert not [n for n in names if MUTATION_NAMES.search(n)], names

            # A bundle edit to the written concept becomes a visible conflict.
            source = bundle / "tables" / "orders.md"
            source.write_text(source.read_text().replace("\n## ", "\nA line added outside the proposal.\n\n## ", 1))
            panel.get_by_role("button", name="Refresh", exact=True).click()
            expect(panel.get_by_role("heading", name=re.compile(r"^Conflicts · [1-9]"))).to_be_visible()
            conflict = panel.locator(".okf-proposal-conflict").first
            expect(conflict.locator(".okf-proposal-conflict__title")).to_have_text(
                "The bundle changed after this proposal was created snapshot_changed")
            expect(conflict).to_contain_text("Bundle snapshot changed since proposal creation.")
            expect(panel.locator(".okf-proposal-facts .okf-proposal-pill[data-state='conflicted']")).to_have_text("Conflicted")

            panel.get_by_role("button", name="All proposals").click()
            back_row = panel.locator(".okf-proposal-row__open")
            expect(back_row).to_be_focused()
            expect(back_row.locator(".okf-proposal-pill")).to_have_text("Conflicted")

            # Very wide screens: the drawer becomes a rail that reserves its real width.
            back_row.click()
            expect(page.locator("#okf-proposal-detail-title")).to_be_focused()
            page.set_viewport_size({"width": 2400, "height": 900})
            page.wait_for_function("() => document.body.classList.contains('okf-panel-rail')")
            rail = page.evaluate("""() => ({
                padding: parseFloat(getComputedStyle(document.body).paddingRight),
                width: document.getElementById('okf-panel').getBoundingClientRect().width})""")
            assert rail["width"] == 1080 and abs(rail["padding"] - rail["width"]) < 1, rail

            # Narrow screens: full-width drawer, unified diff only, no page-level overflow.
            page.set_viewport_size({"width": 390, "height": 844})
            expect(panel.get_by_role("group", name="Diff layout")).to_have_count(0)
            expect(panel.locator("table.okf-proposal-diff--unified")).to_have_count(1)
            geometry = page.evaluate("""() => {
                const p = document.getElementById('okf-panel');
                const b = document.getElementById('okf-panel-body');
                return {width: p.getBoundingClientRect().width, vw: window.innerWidth,
                        overflow: b.scrollWidth - b.clientWidth};
            }""")
            assert abs(geometry["width"] - geometry["vw"]) < 2, geometry
            assert geometry["overflow"] <= 1, geometry

            page.keyboard.press("Escape")
            expect(panel).to_be_hidden()
            _assert_read_only_traffic(requests, proposal_token)
            assert errors == []
        finally:
            context.close()


@pytest.mark.browser
def test_open_list_refetches_on_proposal_event_pages_and_filters(tmp_path: Path, browser) -> None:
    bundle = copy_bundle(DEMO, tmp_path / "bundle")
    private_session_root(bundle)
    with live_server(bundle, "--enable-proposal-review") as base:
        _create_proposal(base, bundle, target="tables/orders", summary="Tag orders",
                         op_id="op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", key="A" * 32, tags=["orders"])
        _, proposal_token = _session_tokens(bundle)
        context, page, requests, errors = _page(browser, page_size=1)
        try:
            _open_studio(page, base)
            page.wait_for_function("() => window.okfLoomLive && window.okfLoomLive.state === 'online'")
            # The Studio home work group carries the registered panel entry.
            page.get_by_role("button", name="Proposals", exact=True).click()
            panel = page.locator("#okf-panel")
            rows = panel.locator(".okf-proposal-row__open")
            expect(rows).to_have_count(1)
            expect(panel.get_by_role("button", name="Load more proposals")).to_have_count(0)

            # A new proposal notifies over SSE; the open list refetches the read model.
            _create_proposal(base, bundle, target="tables/customers", summary="Tag customers",
                             op_id="op_01J2Y8N5Z9Q3A7K6M4T1V0WXZ1", key="B" * 32, tags=["customers"])
            more = panel.get_by_role("button", name="Load more proposals")
            expect(more).to_be_visible(timeout=15000)
            expect(panel.locator("p[role=status]")).to_contain_text("Proposal list updated.")
            expect(rows).to_have_count(1)
            more.focus()
            page.keyboard.press("Enter")
            expect(rows).to_have_count(2)
            expect(rows.nth(1)).to_be_focused()
            expect(panel.get_by_role("button", name="Load more proposals")).to_have_count(0)
            summaries = sorted(rows.locator(".okf-proposal-row__summary").all_inner_texts())
            assert summaries == ["Tag customers", "Tag orders"], summaries

            # A filter with no matches says so and offers the way back.
            panel.get_by_label("Show").select_option("conflicted")
            expect(page.locator("#okf-proposals-list-title")).to_have_text("Conflicted proposals")
            expect(panel.get_by_role("heading", name="No conflicted proposals")).to_be_visible()
            panel.get_by_role("button", name="Show all states").click()
            expect(rows).to_have_count(1)
            expect(panel.get_by_label("Show")).to_be_focused()
            expect(panel.get_by_label("Show")).to_have_value("")

            list_reads = [r.url for r in _proposal_requests(requests) if "/revisions/" not in r.url]
            assert all("limit=1" in url for url in list_reads), list_reads
            assert any("state=conflicted" in url for url in list_reads), list_reads
            assert any("cursor=" in url for url in list_reads), list_reads
            _assert_read_only_traffic(requests, proposal_token)
            assert errors == []
        finally:
            context.close()


@pytest.mark.browser
def test_pagehide_drops_proposal_text_and_restore_refetches(tmp_path: Path, browser) -> None:
    """Pagehide empties open, detached and closed panels; restore reads again."""
    bundle = copy_bundle(DEMO, tmp_path / "bundle")
    private_session_root(bundle)
    markers = ["X3 privacy summary", "X3 privacy rationale marker.", "x3-privacy-marker"]
    with live_server(bundle, "--enable-proposal-review") as base:
        _create_proposal(base, bundle, target="tables/orders", summary=markers[0], rationale=markers[1],
                         op_id="op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", key="A" * 32, tags=[markers[2]])
        _, proposal_token = _session_tokens(bundle)
        context, page, requests, errors = _page(browser)
        try:
            _open_studio(page, base)
            page.get_by_role("button", name="Inspect proposals").click()
            panel = page.locator("#okf-panel")
            panel.locator(".okf-proposal-row__open").click()
            title = page.locator("#okf-proposal-detail-title")
            expect(title).to_have_text(markers[0])
            panel.get_by_text("Full proposed-after text", exact=True).click()
            after = panel.locator("pre[aria-label='Full proposed-after text']")
            expect(after).to_contain_text(markers[2])
            assert _leaked(page, markers) == markers  # the probe sees rendered text

            # 1. Open panel: pagehide empties it synchronously, restore rebuilds
            #    the same detail and refetches it canonically.
            _page_transition(page, "pagehide")
            assert _leaked(page, markers) == []
            _assert_released(page)
            start = len(requests)
            _page_transition(page, "pageshow")
            _wait_for_get(page, requests, start, DETAIL_ROUTE)
            expect(title).to_have_text(markers[0])
            expect(title).to_be_focused()  # focus was inside the emptied panel
            expect(after).to_contain_text(markers[2])  # open full text is a kept layout choice

            # 2. Detached root: another panel replaced the proposal DOM, which
            #    the page still referenced until pagehide.
            page.evaluate("() => window.okfLoomStudio.openPanel('studio')")
            expect(page.get_by_role("button", name="Inspect proposals")).to_be_visible()
            assert _leaked(page, markers) == []
            retained = page.evaluate("() => window.okfLoomStudio._proposalRetention()")
            assert retained["root"] and retained["rootText"] > 0 and retained["detail"], retained
            _page_transition(page, "pagehide")
            _assert_released(page)
            _page_transition(page, "pageshow")

            # 3. Closed panel: closing only hides it, so its text stays in the
            #    document until pagehide.
            start = len(requests)
            page.get_by_role("button", name="Inspect proposals").click()
            _wait_for_get(page, requests, start, DETAIL_ROUTE)
            expect(title).to_have_text(markers[0])
            expect(after).to_contain_text(markers[2])
            page.keyboard.press("Escape")
            expect(panel).to_be_hidden()
            assert _leaked(page, markers) == markers
            _page_transition(page, "pagehide")
            assert _leaked(page, markers) == []
            _assert_released(page)
            _page_transition(page, "pageshow")
            expect(panel).to_be_hidden()
            start = len(requests)
            page.get_by_role("button", name="Studio", exact=False).click()
            page.get_by_role("button", name="Inspect proposals").click()
            _wait_for_get(page, requests, start, DETAIL_ROUTE)
            expect(title).to_have_text(markers[0])
            _assert_read_only_traffic(requests, proposal_token)
            assert errors == []
        finally:
            context.close()


@pytest.mark.browser
def test_connection_recovery_refetches_visible_view_keeping_selection_and_focus(tmp_path: Path, browser) -> None:
    """A dropped and recovered live stream refetches the open list/detail.

    Chromium's offline emulation leaves an in-flight localhost SSE stream up,
    so the drop fails the current EventSource at its boundary (see
    TRACK_EVENT_SOURCES); reconnection is the real live.js path.
    """
    bundle = copy_bundle(DEMO, tmp_path / "bundle")
    private_session_root(bundle)
    with live_server(bundle, "--enable-proposal-review") as base:
        _create_proposal(base, bundle, target="tables/orders", summary="Tag orders",
                         op_id="op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", key="A" * 32, tags=["orders"])
        _, proposal_token = _session_tokens(bundle)
        context, page, requests, errors = _page(browser, track_sources=True)
        try:
            _open_studio(page, base)
            page.wait_for_function("() => window.okfLoomLive && window.okfLoomLive.state === 'online'")
            # Count every hub event that could legitimately cause a refetch, so
            # the refetch below is attributable to recovery alone.
            page.evaluate("""() => {
                window.__x3Events = {};
                for (const type of ['proposal', 'resync', 'changed', 'created', 'removed']) {
                    window.__x3Events[type] = 0;
                    window.okfLoomLive.on(type, () => { window.__x3Events[type]++; });
                }
            }""")

            def drop_and_restore(route: re.Pattern) -> None:
                opened = page.evaluate("() => window.__x3Sources.length")
                state = page.evaluate("""() => {
                    const source = window.__x3Sources[window.__x3Sources.length - 1];
                    source.close();
                    source.dispatchEvent(new Event('error'));
                    return window.okfLoomLive.state;
                }""")
                assert state == "reconnecting", state
                # live.js reconnects only after its backoff timer, so nothing
                # below this index can predate the drop.
                start = len(requests)
                page.wait_for_function("() => window.okfLoomLive.state === 'online'", timeout=30000)
                assert page.evaluate("() => window.__x3Sources.length") > opened  # a new real stream
                _wait_for_get(page, requests, start, route)

            page.get_by_role("button", name="Proposals", exact=True).click()
            panel = page.locator("#okf-panel")
            row = panel.locator(".okf-proposal-row__open")
            expect(row).to_have_count(1)
            row.focus()
            drop_and_restore(LIST_ROUTE)
            expect(panel.locator(".okf-proposals__body")).to_have_attribute("aria-busy", "false")
            expect(row).to_have_count(1)
            expect(row).to_be_focused()

            row.click()
            title = page.locator("#okf-proposal-detail-title")
            expect(title).to_have_text("Tag orders")
            full = panel.get_by_text("Full proposed-after text", exact=True)
            full.click()
            expect(full).to_be_focused()
            drop_and_restore(DETAIL_ROUTE)
            expect(panel.locator(".okf-proposals__body")).to_have_attribute("aria-busy", "false")
            expect(title).to_have_text("Tag orders")  # same selection
            expect(full).to_be_focused()  # focus kept on the same control
            expect(panel.locator("pre[aria-label='Full proposed-after text']")).to_be_visible()

            assert page.evaluate("() => window.__x3Events") == {
                "proposal": 0, "resync": 0, "changed": 0, "created": 0, "removed": 0}
            _assert_read_only_traffic(requests, proposal_token)
            assert errors == []
        finally:
            context.close()


@pytest.mark.browser
def test_applied_proposal_never_claims_missing_apply_history(tmp_path: Path, browser) -> None:
    bundle = copy_bundle(DEMO, tmp_path / "bundle")
    op_id = "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
    # Apply publishes only over owner-private (0600) targets, as in
    # tests/test_proposal_cli.py; set that before the proposal snapshot.
    (bundle / "tables" / "orders.md").chmod(0o600)
    private_session_root(bundle)
    with live_server(bundle, "--enable-proposal-review") as base:
        created = _create_proposal(base, bundle, target="tables/orders", summary="Tag orders",
                                   op_id=op_id, key="A" * 32, tags=["orders"])
        proposal_id = created["proposal"]["proposal_id"]
        # Decide and apply through the real CLI against the served bundle.
        show = ("proposal", "show", str(bundle), proposal_id, "--revision", "1")
        shown = _cli(*show)
        _cli("proposal", "decide", str(bundle), proposal_id, "--revision", "1", "--op", op_id,
             "--decision", "accepted", "--actor", "x3-reviewer",
             "--read-model-token", shown["read_model_token"], "--idempotency-key", "B" * 32)
        ready = _cli(*show)
        assert ready["state"] == "ready", ready["state"]
        snapshot = ready["proposal"]["expected_snapshot"]
        applied = _cli("proposal", "apply", str(bundle), proposal_id, "--revision", "1",
                       "--actor", "x3-reviewer", "--snapshot-digest", snapshot["snapshot_digest"],
                       "--validation-context-digest", snapshot["validation_context_digest"],
                       "--decision-set-token", ready["decision_set_token"],
                       "--read-model-token", ready["read_model_token"], "--idempotency-key", "C" * 32)
        assert applied["status"] == "applied", applied
        _, proposal_token = _session_tokens(bundle)
        context, page, requests, errors = _page(browser)
        try:
            _open_studio(page, base)
            page.get_by_role("button", name="Inspect proposals").click()
            panel = page.locator("#okf-panel")
            row = panel.locator(".okf-proposal-row__open")
            expect(row.locator(".okf-proposal-pill")).to_have_text("Applied")
            row.click()
            expect(page.locator("#okf-proposal-detail-title")).to_have_text("Tag orders")
            expect(panel.locator(".okf-proposal-facts .okf-proposal-pill[data-state='applied']")).to_have_text("Applied")
            expect(panel.locator(".okf-proposal-table__meta").filter(has_text="by x3-reviewer (unverified)")).to_have_count(1)
            lifecycle = panel.locator("section[aria-labelledby='okf-proposal-sec-lifecycle']")
            expect(lifecycle.get_by_text(NOT_REPORTED, exact=True)).to_be_visible()
            expect(lifecycle).not_to_contain_text("No apply or undo attempts")
            expect(lifecycle).not_to_contain_text("Undo available")
            _assert_read_only_traffic(requests, proposal_token)
            assert errors == []
        finally:
            context.close()


@pytest.mark.parametrize("target", ["spa", "static", "single-file"])
def test_exports_never_load_the_proposals_panel(tmp_path: Path, target: str) -> None:
    bundle = copy_bundle(DEMO, tmp_path / "bundle")
    out = tmp_path / target
    subprocess.run(okf_module_argv("build", str(bundle), "--target", target, "--out", str(out)),
                   cwd=ROOT, env=okf_subprocess_env(), check=True, capture_output=True, timeout=120)
    pages = list(out.rglob("*.html"))
    assert pages
    for html in pages:
        text = html.read_text(errors="replace")
        assert not re.search(r"<script[^>]+studio\.js", text), html
        assert 'id="okf-studio-bootstrap"' not in text, html
        assert "/__proposal/" not in text, html
        assert "okf-proposals" not in text, html
