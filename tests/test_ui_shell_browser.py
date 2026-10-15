"""Browser proof for the shared shell and consolidated Studio surface."""
from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from browser_support import (expect, sync_playwright, launch_chromium, copy_bundle, live_server, reveal_control,
                             managed_server, free_port)

from conftest import okf_module_argv, okf_subprocess_env

pytestmark = pytest.mark.browser
TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DEMO_BUNDLE = TOOLKIT_ROOT / "samples" / "demo_bundle"


def _capture_evidence(page, name: str) -> None:
    root = os.environ.get("OKF_UI_CLOSEOUT_PROOF_DIR")
    if not root:
        return
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / name), full_page=True)


def _install_error_probe(page) -> list[str]:
    failures: list[str] = []
    page.on("console", lambda message: failures.append(f"console: {message.text}") if message.type == "error" else None)
    page.on("requestfailed", lambda request: failures.append(f"requestfailed: {request.url}")
            if not re.search(r"/(?:__events|__presence)(?:\?|$)", request.url) else None)
    page.on("response", lambda response: failures.append(f"http {response.status}: {response.url}") if response.status >= 400 else None)
    return failures


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def server_url(tmp_path_factory) -> str:
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("shell") / "bundle")
    with live_server(bundle) as base:
        yield base


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        try:
            yield context.new_page()
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("route", ["/missing-concept", "/__feedback-import"])
@pytest.mark.parametrize("choice,dark,expected", [("plum", False, "plum"), ("system", True, "slate")])
def test_auxiliary_pages_resolve_saved_appearance_before_first_paint(page, server_url, route, choice, dark, expected):
    page.emulate_media(color_scheme="dark" if dark else "light")
    page.add_init_script("localStorage.setItem('okf-loom.appearance', " + repr(choice) + ");"
                         "requestAnimationFrame(() => { window.__firstPalette = document.documentElement.dataset.theme; });")
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" and "Content Security Policy" in msg.text else None)
    response = page.goto(server_url + route)
    assert response.status == (404 if route.startswith('/missing') else 200)
    page.wait_for_function("() => window.__firstPalette && window.OKFLoomTheme")
    assert page.evaluate("window.__firstPalette") == expected
    expect(page.locator('html')).to_have_attribute('data-theme', expected)
    scripts = page.locator('head script[src$="/theme.js"]')
    expect(scripts).to_have_count(1)
    assert scripts.get_attribute('defer') is None and scripts.get_attribute('async') is None
    assert page.evaluate("() => { const config = document.getElementById('okf-theme-config');"
                         "return !!config && !!(config.compareDocumentPosition(document.querySelector('script[src$=\"/theme.js\"]')) & Node.DOCUMENT_POSITION_FOLLOWING); }")
    page.locator('#okf-theme').click()
    page.locator('label[for="okf-appearance-theme-clay"]').click()
    expect(page.locator('html')).to_have_attribute('data-theme', 'clay')
    assert errors == []


def test_composer_outcomes_use_square_two_pixel_status_bars(page, server_url):
    page.goto(server_url + '/tables/orders')
    page.wait_for_function('() => window.okfStudioReadiness && window.okfStudioReadiness.ready')
    page.locator('.okf-studio-launcher').click()
    page.locator('#okf-panel').get_by_role('button', name='Comments', exact=True).click()
    held = {}
    page.route('**/__comment', lambda route: held.update(route=route))
    page.get_by_role('textbox', name='Comment for agent').fill('Status styling proof.')
    page.get_by_role('button', name='Send', exact=True).click()
    status = page.locator('.okf-composer__status')
    expect(status).to_have_attribute('data-tone', 'info')
    def check_bar(tone):
        painted = status.evaluate("""(n, tone) => {
          const s = getComputedStyle(n), p = document.createElement('i');
          p.style.color = `var(--status-${tone})`; document.body.append(p);
          const colour = getComputedStyle(p).color; p.remove();
          return {inline: n.getAttribute('style'), radius: s.borderRadius, shadow: s.boxShadow,
            colour, bg: s.backgroundColor};
        }""", tone)
        assert painted['inline'] is None and painted['radius'] == '0px', painted
        assert painted['shadow'] == painted['colour'] + ' 2px 0px 0px 0px inset', painted
        assert painted['bg'] == 'rgba(0, 0, 0, 0)', painted
    check_bar('info')
    assert 'route' in held
    held['route'].fulfill(status=403, content_type='application/json', body='{"ok":false,"error":"forbidden"}')
    expect(status).to_have_attribute('data-tone', 'error')
    check_bar('error')


def test_forced_colours_toast_retains_a_complete_frame(page, server_url):
    page.emulate_media(forced_colors='active')
    page.goto(server_url + '/')
    page.wait_for_function('() => window.okfStudioReadiness && window.okfStudioReadiness.ready')
    page.evaluate("window.okfLoomStudio._toast('Status message', {tone:'warn',sticky:true})")
    toast = page.locator('.okf-toast').first
    assert toast.evaluate("""n => { const s = getComputedStyle(n);
      return ['Top','Right','Bottom','Left'].every(side => parseFloat(s['border' + side + 'Width']) >= 1
        && s['border' + side + 'Style'] === 'solid' && s['border' + side + 'Color'] !== s.backgroundColor); }""")


def test_focus_is_a_neutral_modifier_and_removes_the_empty_tools_band(page, server_url):
    page.goto(server_url + '/tables/orders')
    page.wait_for_function('() => window.okfStudioReadiness && window.okfStudioReadiness.ready')
    focus = page.get_by_role('button', name='Focus view', exact=True)
    focus.click()
    expect(focus).to_have_attribute('aria-pressed', 'true')
    assert not page.locator('.okf-document-tools').is_visible()
    styles = page.locator('.okf-viewswitch__btn[aria-pressed="true"]').evaluate_all("""buttons => buttons.map(b => {
      const s = getComputedStyle(b); return {name: b.textContent, weight: s.fontWeight,
        bar: s.borderBottomColor, square: getComputedStyle(b, '::before').backgroundColor}; })""")
    active = next(s for s in styles if s['name'] == 'Rendered')
    toggle = next(s for s in styles if s['name'] == 'Focus view')
    assert active['weight'] == '700' and toggle['weight'] == '500', styles
    assert toggle['bar'] == 'rgba(0, 0, 0, 0)' and toggle['square'] != 'rgba(0, 0, 0, 0)', styles


@pytest.mark.parametrize('width', [1440, 390])
def test_drawer_quick_actions_stack_and_buttons_share_the_control_standard(page, server_url, width):
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(server_url + '/tables/orders')
    page.wait_for_function('() => window.okfStudioReadiness && window.okfStudioReadiness.ready')
    page.locator('.okf-studio-launcher').click()
    actions = page.locator('#okf-panel .okf-studio-quick-action')
    assert actions.count() > 0
    assert actions.evaluate_all("""nodes => nodes.every(n => {
      const [intent, outcome] = n.children;
      return outcome.getBoundingClientRect().top >= intent.getBoundingClientRect().bottom - 1;
    })""")
    activity = page.locator('#okf-panel').get_by_role('button', name='Agent activity', exact=True)
    assert activity.evaluate("b => getComputedStyle(b).borderTopStyle") == 'solid'
    page.locator('#okf-panel').get_by_role('button', name='Comments', exact=True).click()
    toggle = page.get_by_role('button', name='Show archived', exact=True)
    send = page.get_by_role('button', name='Send', exact=True)
    assert toggle.evaluate("b => getComputedStyle(b).fontSize") == send.evaluate("b => getComputedStyle(b).fontSize")
    assert toggle.bounding_box()['height'] == send.bounding_box()['height']


def test_phone_attention_does_not_add_a_third_shell_row(page, server_url):
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto(server_url + '/')
    page.wait_for_function('() => window.okfLoomStudio && window.okfLoomStudio.state.eventsReady')
    page.evaluate("window.okfLoomLive.emit('activity', {event_id:'repair-attention',type:'activity',action:'update',ts:new Date().toISOString()})")
    launcher = page.locator('.okf-studio-launcher')
    expect(launcher).to_have_attribute('data-attention', 'pending')
    expect(launcher).to_have_accessible_name(re.compile(r'Needs attention'))
    assert page.locator('.okf-shell').bounding_box()['height'] <= 115
    assert launcher.bounding_box()['y'] == page.locator('.okf-shell__search-form').bounding_box()['y']


def test_recent_pages_popover_is_anchored_below_the_search_field(page, server_url):
    page.goto(server_url + '/tables/orders')
    page.locator('.okf-shell__search-form input').focus()
    menu = page.locator('.okf-search-live')
    expect(menu).to_be_visible()
    field = page.locator('.okf-shell__search-form').bounding_box()
    popup = menu.bounding_box()
    assert abs(popup['x'] - field['x']) < 1
    assert abs(popup['y'] - field['y'] - field['height']) < 1
    assert popup['width'] < 400


_FIRST_PAINT_CLS = """(() => {
  const shifts = window.__okfPaintShifts = [];
  const record = entries => entries.forEach(entry => {
    if (!entry.hadRecentInput) shifts.push({value: entry.value,
      sources: (entry.sources || []).map(source => ({
        node: source.node && (source.node.id || source.node.className),
        before: source.previousRect.toJSON(), after: source.currentRect.toJSON()
      }))});
  });
  const observer = new PerformanceObserver(list => record(list.getEntries()));
  observer.observe({type: 'layout-shift', buffered: true});
  window.__okfPaintCLS = () => {
    record(observer.takeRecords());
    return {cls: shifts.reduce((sum, shift) => sum + shift.value, 0), shifts};
  };
})();"""


@pytest.mark.parametrize("route", ["/", "/tables/orders"])
@pytest.mark.parametrize("width,coarse", [(1280, False), (390, False), (320, False),
                                         (390, True), (320, True)])
def test_live_first_paint_stays_stable_after_studio_boot(server_url, route, width, coarse):
    """Hold the real module until after first paint, so a fast local server
    cannot conceal a post-paint insertion. Measure after async boot settles."""
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": width, "height": 900},
                                      is_mobile=coarse, has_touch=coarse)
        try:
            context.add_init_script(_FIRST_PAINT_CLS)
            page = context.new_page()
            held = []
            page.route("**/__static/studio.js", lambda request: held.append(request))
            page.goto(server_url + route, wait_until="commit")
            page.wait_for_function("""() => document.querySelector('.okf-shell__studio-slot button') &&
              performance.getEntriesByType('paint').some(entry => entry.name === 'first-contentful-paint')""")
            assert held
            expect(page.locator('.okf-studio-launcher')).to_be_hidden()
            before = page.locator('.okf-shell').bounding_box()
            if route != "/":
                assert page.locator('[data-okf-viewbar-placeholder]').bounding_box()["height"] >= 42
                expect(page.locator('[data-okf-viewbar-placeholder]')).to_be_hidden()
            held[0].continue_()
            page.wait_for_function("""() => window.okfStudioReadiness?.ready &&
              window.okfLoomStudio.state.commentsReady && window.okfLoomStudio.state.graphReady""")
            page.evaluate("() => document.fonts.ready")
            page.wait_for_timeout(600)  # includes Studio's 400ms layout-settle pass
            after = page.locator('.okf-shell').bounding_box()
            assert after["height"] == before["height"], (before, after)
            result = page.evaluate("window.__okfPaintCLS()")
            assert result["cls"] < 0.02, result
            assert page.locator('[data-okf-viewbar-placeholder], [data-okf-page-feedback-placeholder], '
                                '[data-okf-outline-jump-placeholder]').count() == 0
            if route != "/":
                assert page.locator('.okf-concept-viewbar [data-okf-outline-jump]').count() == 1
                # View tabs on one row share one label line: no tab (Split was
                # inline-flex below 900px) pins its label to the top of the
                # 44px coarse target while its neighbours centre theirs.
                tabs = page.evaluate("""() => [...document.querySelectorAll('.okf-concept-viewbar .okf-viewswitch__btn')]
                  .filter(b => b.getClientRects().length).map(b => {
                    const box = b.getBoundingClientRect(), text = document.createRange();
                    text.selectNodeContents(b);
                    return {label: b.textContent.trim(), top: box.top, height: box.height,
                            textTop: text.getBoundingClientRect().top};
                  })""")
                assert {"Rendered", "Source", "Split"} <= {tab["label"] for tab in tabs}, tabs
                rows: dict[int, list] = {}
                for tab in tabs:
                    rows.setdefault(round(tab["top"]), []).append(tab)
                for row in rows.values():
                    offsets = [tab["textTop"] - tab["top"] for tab in row]
                    assert max(offsets) - min(offsets) <= 0.5, tabs
                    assert len({round(tab["height"], 1) for tab in row}) == 1, tabs
                if coarse:
                    assert all(tab["height"] >= 43.5 for tab in tabs), tabs
            expect(page.locator('.okf-studio-launcher')).to_be_visible()
            assert page.evaluate("matchMedia('(pointer: coarse)').matches") is coarse
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        finally:
            context.close()
            browser.close()


_BOOT_NOTICE_STATE = """() => {
  const bar = document.querySelector('.okf-topbar.okf-shell');
  const notice = [...document.querySelectorAll('.okf-live-studio-fallback')]
    .find(n => n.getClientRects().length && getComputedStyle(n).display !== 'none');
  const box = notice ? notice.getBoundingClientRect() : null;
  const controls = [...bar.querySelectorAll('a, button, input, select, summary')].filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden';
  });
  // Every visible top-bar control is the topmost element at its own centre.
  const covered = controls.filter(e => {
    const r = e.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return !hit || !(hit === e || e.contains(hit));
  }).map(e => e.getAttribute('aria-label') || e.textContent.trim());
  const hit = box && document.elementFromPoint(box.left + Math.min(40, box.width / 2), box.top + box.height / 2);
  return {
    barBottom: bar.getBoundingClientRect().bottom,
    notice: box && {top: box.top, height: box.height, position: getComputedStyle(notice).position,
                    topmost: !!(hit && notice.contains(hit)), text: notice.textContent.trim()},
    controls: controls.length, covered,
    reserved: [...document.querySelectorAll('[data-okf-viewbar-placeholder], [data-okf-page-feedback-placeholder], '
      + '.okf-shell__studio-slot[hidden]')].filter(e => e.getClientRects().length).map(e => e.className),
    status: document.documentElement.dataset.okfStudioStatus || null,
    code: document.documentElement.dataset.okfStudioCode || null,
    overflow: document.documentElement.scrollWidth > innerWidth + 1,
  };
}"""


@pytest.mark.parametrize("route", ["/", "/tables/orders", "/__graph"])
@pytest.mark.parametrize("width,coarse", [(1280, False), (390, True), (320, True)])
def test_live_boot_notice_sits_below_the_top_bar_while_studio_loads(server_url, route, width, coarse):
    """While Studio loads, the truthful notice is visible directly under the
    top bar, out of flow: it covers no bar control (it once sat at y=0 over
    the brand, search and destinations, and under the narrow graph bar), and
    boot removes it without shifting the page."""
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": width, "height": 900},
                                      is_mobile=coarse, has_touch=coarse)
        try:
            context.add_init_script(_FIRST_PAINT_CLS)
            context.add_init_script("try { localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}")
            page = context.new_page()
            held = []
            page.route("**/__static/studio.js", lambda request: held.append(request))
            page.goto(server_url + route, wait_until="commit")
            page.wait_for_function("""() => performance.getEntriesByType('paint').some(e => e.name === 'first-contentful-paint')
              && [...document.querySelectorAll('.okf-live-studio-fallback')].some(n => n.getClientRects().length)""")
            page.wait_for_timeout(300)
            loading = page.evaluate(_BOOT_NOTICE_STATE)
            assert loading["status"] is None and loading["notice"], loading
            assert loading["notice"]["position"] == "absolute" and loading["notice"]["topmost"], loading
            assert abs(loading["notice"]["top"] - loading["barBottom"]) <= 1, loading
            assert loading["controls"] and loading["covered"] == [], loading
            assert held
            held[0].continue_()
            page.wait_for_function("() => window.okfStudioReadiness?.ready && window.okfLoomStudio.state.commentsReady")
            page.evaluate("() => document.fonts.ready")
            page.wait_for_timeout(600)
            ready = page.evaluate(_BOOT_NOTICE_STATE)
            assert ready["notice"] is None and ready["covered"] == [] and not ready["overflow"], ready
            if not (route == "/__graph" and width <= 640):
                # The narrow graph route's own chrome still moves after first
                # paint (pre-existing, owned by graph.js); the notice adds none.
                result = page.evaluate("window.__okfPaintCLS()")
                assert result["cls"] < 0.02, result
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("route", ["/", "/tables/orders", "/__graph"])
@pytest.mark.parametrize("width,coarse", [(1280, False), (390, True), (320, True)])
def test_blocked_studio_shows_the_failure_below_the_bar_and_collapses_reserved_slots(
    server_url, route, width, coarse,
):
    """A Studio module that never runs is a terminal failure: theme.js marks it
    (blocked-script), the notice joins the flow directly below the bar, so it
    covers neither bar controls nor content, and the launcher, viewbar and
    feedback slots it will never fill collapse instead of staying blank."""
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": width, "height": 900},
                                      is_mobile=coarse, has_touch=coarse)
        try:
            context.add_init_script("try { localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}")
            page = context.new_page()
            page.route("**/__static/studio.js", lambda request: request.abort())
            page.goto(server_url + route, wait_until="load")
            page.wait_for_function("() => document.documentElement.dataset.okfStudioStatus === 'failed'")
            page.wait_for_timeout(300)
            failed = page.evaluate(_BOOT_NOTICE_STATE)
            assert failed["code"] == "blocked-script", failed
            assert failed["notice"] and failed["notice"]["text"].startswith("Live Studio is unavailable."), failed
            assert failed["notice"]["position"] == "static" and failed["notice"]["topmost"], failed
            assert abs(failed["notice"]["top"] - failed["barBottom"]) <= 1, failed
            assert failed["controls"] and failed["covered"] == [], failed
            assert failed["reserved"] == [] and not failed["overflow"], failed
            readiness = page.evaluate("() => window.okfStudioReadiness")
            assert readiness["status"] == "failed" and readiness["ready"] is False, readiness
        finally:
            context.close()
            browser.close()


@pytest.fixture(scope="module")
def read_only_server_url(tmp_path_factory) -> str:
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("shell-read-only") / "bundle")
    with live_server(bundle, "--no-edit") as base:
        yield base


@pytest.mark.parametrize("width,coarse", [(1280, False), (390, False), (320, False), (390, True), (320, True)])
def test_read_only_index_feedback_slot_reserves_the_read_only_layout(read_only_server_url, width, coarse):
    """A session that cannot comment mounts View comments plus a read-only
    note, not the Comment button. The reserved slot takes that layout before
    first paint, so boot does not shift the index (it was 32px -> 78px)."""
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": width, "height": 900},
                                      is_mobile=coarse, has_touch=coarse)
        try:
            context.add_init_script(_FIRST_PAINT_CLS)
            page = context.new_page()
            held = []
            page.route("**/__static/studio.js", lambda request: held.append(request))
            page.goto(read_only_server_url + "/", wait_until="commit")
            page.wait_for_function("""() => document.querySelector('[data-okf-page-feedback-placeholder]') &&
              performance.getEntriesByType('paint').some(entry => entry.name === 'first-contentful-paint')""")
            assert page.evaluate("document.documentElement.dataset.okfStudioComment") == "0"
            reserved = page.locator("[data-okf-page-feedback-placeholder]").bounding_box()
            held[0].continue_()
            page.wait_for_function("""() => window.okfStudioReadiness?.ready &&
              window.okfLoomStudio.state.commentsReady && !document.querySelector('[data-okf-page-feedback-placeholder]')""")
            page.evaluate("() => document.fonts.ready")
            page.wait_for_timeout(600)
            group = page.get_by_role("group", name="Page feedback")
            expect(group.get_by_role("button", name="View comments")).to_be_visible()
            expect(group.get_by_role("button", name="Comment on this page")).to_have_count(0)
            expect(group.locator(".okf-page-feedback__note")).to_have_text(
                "This session is read-only: comments can be read here but not added.")
            mounted = group.bounding_box()
            assert abs(mounted["height"] - reserved["height"]) <= 1 and abs(mounted["y"] - reserved["y"]) <= 1, (reserved, mounted)
            result = page.evaluate("window.__okfPaintCLS()")
            assert result["cls"] < 0.02, result
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("route", ["/", "/tables/orders"])
def test_nojs_does_not_reserve_studio_controls(server_url, route):
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(java_script_enabled=False, viewport={"width": 320, "height": 900})
        try:
            page = context.new_page()
            page.goto(server_url + route)
            expect(page.locator('[data-okf-shell-item="studio"]')).to_be_hidden()
            expect(page.locator('.okf-studio-launcher')).to_be_hidden()
            assert page.locator('.okf-concept-viewbar:visible').count() == 0
            expect(page.locator('.okf-nojs-live-banner')).to_be_visible()
            assert page.locator('[data-okf-shell-item="studio"]').bounding_box() is None
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("studio_mode", ["enabled", "cli-disabled", "config-disabled"])
@pytest.mark.parametrize("width", [1280, 390])
def test_inactive_studio_and_documentless_indexes_leave_no_reserved_bands(tmp_path, page, studio_mode, width):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "a.md").write_text('---\ntype: note\n---\n# Service\n\nBody\n')
    flags = ()
    if studio_mode == "cli-disabled":
        flags = ("--no-edit", "--no-watch-ui")
    elif studio_mode == "config-disabled":
        (tmp_path / "okf-loom.config.yaml").write_text('studio:\n  live: false\n  edit: false\n')
    page.set_viewport_size({"width": width, "height": 900})
    with live_server(tmp_path, *flags) as base:
        routes = ["/", "/services/"]
        if studio_mode != "enabled":
            routes += ["/services/a", "/__graph", "/__search?q=Service"]
        for route in routes:
            page.goto(base + route, wait_until="load")
            if studio_mode == "enabled":
                page.wait_for_function("() => window.okfStudioReadiness?.ready")
                expect(page.locator('.okf-studio-launcher')).to_be_visible()
            else:
                assert page.locator('#okf-studio-bootstrap').count() == 0
                assert page.locator('.okf-studio-launcher').count() == 0
            page.evaluate("() => document.fonts.ready")
            page.wait_for_timeout(600)
            reserved = page.evaluate("""() => [...document.querySelectorAll(
              '[data-okf-viewbar-placeholder], [data-okf-outline-jump-placeholder], '
              + '[data-okf-page-feedback-placeholder], .okf-shell__studio-slot[hidden]')]
              .filter(n => n.getBoundingClientRect().height > 0).map(n => n.outerHTML)""")
            assert reserved == [], (studio_mode, route, reserved)
            assert page.locator('.okf-hero--feedback').count() == 0


def test_studio_removes_feedback_placeholder_when_no_document_target(tmp_path, page):
    (tmp_path / 'services').mkdir()
    (tmp_path / 'services' / 'a.md').write_text('---\ntype: note\n---\nBody\n')
    def add_unfillable_slot(route):
        response = route.fetch()
        body = response.text().replace('class="okf-hero ', 'class="okf-hero okf-hero--feedback ', 1)
        body = body.replace('</main>', '<div class="okf-page-feedback" '
                            'data-okf-page-feedback-placeholder aria-hidden="true">Unused</div></main>', 1)
        route.fulfill(response=response, body=body)
    page.route('**/services/', add_unfillable_slot)
    with live_server(tmp_path) as base:
        page.goto(base + '/services/')
        page.wait_for_function("() => window.okfStudioReadiness?.ready")
        assert page.locator('[data-okf-page-feedback-placeholder], .okf-hero--feedback').count() == 0


def test_studio_mounts_one_compact_launcher_in_the_shell(page, server_url: str) -> None:
    page.goto(server_url + "/")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")

    slot = page.locator('[data-okf-shell-item="studio"]')
    launcher = slot.get_by_role("button", name="Studio")
    assert launcher.count() == 1
    assert launcher.is_visible()
    assert page.locator(".okf-studio-bar").count() == 0


@pytest.mark.parametrize("width", [1440, 390])
def test_index_feedback_header_stays_compact_in_bone(page, server_url: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server_url + "/")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    page.evaluate("() => window.OKFLoomTheme.setTheme('bone')")
    feedback = page.get_by_role("button", name="Comment on this page", exact=True)
    expect(feedback).to_be_visible()
    metrics = feedback.evaluate("""button => {
      const range = document.createRange();
      range.selectNodeContents(button);
      const group = button.closest('.okf-page-feedback');
      return {lines: range.getClientRects().length, height: group.getBoundingClientRect().height,
        right: group.getBoundingClientRect().right, width: innerWidth,
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
    }""")
    assert metrics["lines"] == 1, metrics
    assert metrics["height"] <= 48, metrics
    assert metrics["right"] <= width + 1 and metrics["overflow"] <= 1, metrics
    _capture_evidence(page, f"index-feedback-bone-{width}.png")


def test_visible_shell_and_graph_preview_labels_are_preserved_in_accessible_names(
    page, server_url: str,
) -> None:
    failures = _install_error_probe(page)
    page.goto(server_url + "/")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    checks = page.evaluate("""() => {
      const normalized = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const row = selector => {
        const node = document.querySelector(selector);
        return {visible: normalized(node.innerText), name: normalized(node.getAttribute('aria-label') || node.innerText)};
      };
      return {
        studio: row('.okf-studio-launcher'),
        appearance: row('#okf-theme'),
        graph: (() => {
          const node = document.querySelector('.okf-graph-thumb__link');
          return {visible: normalized(node.querySelector('figcaption').innerText),
            name: normalized(node.querySelector('.sr-only').textContent + ' ' + node.querySelector('figcaption').innerText)};
        })()
      };
    }""")
    assert checks["studio"]["visible"] in checks["studio"]["name"]
    assert checks["appearance"]["visible"] in checks["appearance"]["name"]
    assert checks["graph"]["visible"] in checks["graph"]["name"]
    _capture_evidence(page, "desktop-index-accessible-names.png")
    assert failures == []


def test_graph_summary_uses_ordered_headings(page, server_url: str) -> None:
    failures = _install_error_probe(page)
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.okfGraphCaptureState && window.okfGraphCaptureState().layoutSettled")
    assert page.get_by_role("heading", name="Suggested starting points", level=2).count() == 1
    levels = page.locator("h1:visible,h2:visible,h3:visible,h4:visible,h5:visible,h6:visible").evaluate_all(
        "nodes => nodes.map(node => Number(node.tagName.slice(1)))"
    )
    assert all(current <= previous + 1 for previous, current in zip(levels, levels[1:])), levels
    _capture_evidence(page, "desktop-graph-heading-order.png")
    assert failures == []


@pytest.mark.parametrize("query, expected_type", [("checkout", "Service"), ("refund", "Playbook")])
def test_search_type_labels_meet_text_contrast_in_live_suggestions(
    page, server_url: str, query: str, expected_type: str,
) -> None:
    failures = _install_error_probe(page)
    page.goto(server_url + "/")
    search = page.locator('.okf-shell__search-form input[type="search"]')
    search.fill(query)
    label = page.locator(".okf-search-live__type-label", has_text=expected_type).first
    expect(label).to_be_visible()
    contrast = label.evaluate("""node => {
      const parse = value => {
        const parts = value.match(/[\\d.]+/g).map(Number);
        if (value.startsWith('color(srgb')) {
          return {rgb: parts.slice(0, 3).map(channel => channel * 255), alpha: parts.length > 3 ? parts[3] : 1};
        }
        return {rgb: parts.slice(0, 3), alpha: parts.length > 3 ? parts[3] : 1};
      };
      const opaqueBackground = element => {
        let current = element;
        let result = [255, 255, 255];
        const layers = [];
        while (current) {
          const color = parse(getComputedStyle(current).backgroundColor);
          if (color.alpha > 0) layers.push(color);
          if (color.alpha >= 1) break;
          current = current.parentElement;
        }
        layers.reverse().forEach(color => {
          result = color.rgb.map((channel, index) => channel * color.alpha + result[index] * (1 - color.alpha));
        });
        return result;
      };
      const linear = rgb => rgb.map(channel => {
          channel /= 255;
          return channel <= .04045 ? channel / 12.92 : Math.pow((channel + .055) / 1.055, 2.4);
        });
      const luminance = rgb => .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];
      const fg = luminance(linear(parse(getComputedStyle(node).color).rgb));
      const bg = luminance(linear(opaqueBackground(node.closest('.okf-search-live__type'))));
      return {ratio: (Math.max(fg, bg) + .05) / (Math.min(fg, bg) + .05),
        foreground: getComputedStyle(node).color,
        background: getComputedStyle(node.closest('.okf-search-live__type')).backgroundColor,
        opaqueBackground: opaqueBackground(node.closest('.okf-search-live__type'))};
    }""")
    assert contrast["ratio"] >= 4.5, (expected_type, contrast)
    search.press("Enter")
    page.wait_for_url(re.compile(r"/__search\?"))
    result_label = page.locator(".okf-search-result__type", has_text=expected_type).first
    expect(result_label).to_be_visible()
    result_contrast = result_label.evaluate("""node => {
      const parse = value => {
        const parts = value.match(/[\\d.]+/g).map(Number);
        if (value.startsWith('color(srgb')) {
          return {rgb: parts.slice(0, 3).map(channel => channel * 255), alpha: parts.length > 3 ? parts[3] : 1};
        }
        return {rgb: parts.slice(0, 3), alpha: parts.length > 3 ? parts[3] : 1};
      };
      const linear = rgb => rgb.map(channel => {
        channel /= 255;
        return channel <= .04045 ? channel / 12.92 : Math.pow((channel + .055) / 1.055, 2.4);
      });
      const luminance = rgb => .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];
      const style = getComputedStyle(node);
      const fg = luminance(linear(parse(style.color).rgb));
      const bg = luminance(linear(parse(getComputedStyle(document.body).backgroundColor).rgb));
      return {ratio: (Math.max(fg, bg) + .05) / (Math.min(fg, bg) + .05),
        foreground: style.color, background: getComputedStyle(document.body).backgroundColor};
    }""")
    assert result_contrast["ratio"] >= 4.5, (expected_type, result_contrast)
    _capture_evidence(page, f"desktop-search-{expected_type.lower()}-contrast.png")
    assert failures == []


def test_mobile_studio_keeps_schema_and_sql_in_contained_horizontal_scrollports(
    page, server_url: str,
) -> None:
    failures = _install_error_probe(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    page.locator(".okf-studio-launcher").click()
    page.locator("#okf-panel").wait_for(state="visible")
    geometry = page.evaluate("""() => {
      const root = document.documentElement;
      const code = document.querySelector('.okf-content-scrollport--code');
      const pre = code && code.querySelector('pre');
      const cellCode = document.querySelector('.okf-table td code');
      const tablePort = document.querySelector('.okf-tablewrap');
      return {
        pageOverflow: root.scrollWidth > root.clientWidth + 1,
        codeLocal: !!code && code.scrollWidth >= code.clientWidth,
        codeWhiteSpace: pre && getComputedStyle(pre).whiteSpace,
        identifierWhiteSpace: cellCode && getComputedStyle(cellCode).whiteSpace,
        tableContained: !!tablePort && tablePort.getBoundingClientRect().right <= root.clientWidth + 1,
      };
    }""")
    assert geometry == {
        "pageOverflow": False,
        "codeLocal": True,
        "codeWhiteSpace": "pre",
        "identifierWhiteSpace": "nowrap",
        "tableContained": True,
    }
    _capture_evidence(page, "mobile-studio-schema-code.png")
    assert failures == []


def test_shell_navigation_focuses_the_new_route_heading(page, server_url: str) -> None:
    page.goto(server_url + "/")
    # There is no standalone Search shortcut any more: an empty search submit
    # is the header's way to open the full search route.
    search = page.locator('.okf-shell__search-form input')
    search.press("Enter")
    page.wait_for_url(lambda url: '/__search' in url)

    assert page.evaluate("document.activeElement === document.querySelector('[data-okf-route-heading]')")


def test_studio_is_a_non_modal_rail_on_desktop_and_restores_its_trigger(page, server_url: str) -> None:
    page.goto(server_url + "/")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    launcher = page.locator(".okf-studio-launcher")
    launcher.click()

    panel = page.locator("#okf-panel")
    assert panel.get_attribute("role") == "complementary"
    assert panel.get_attribute("aria-modal") is None
    assert page.evaluate("document.activeElement === document.querySelector('#okf-panel-title')")

    page.keyboard.press("Escape")
    assert panel.is_hidden()
    assert page.evaluate("document.activeElement === document.querySelector('.okf-studio-launcher')")


def test_studio_reflows_to_a_named_modal_dialog(page, server_url: str) -> None:
    page.set_viewport_size({"width": 600, "height": 800})
    page.goto(server_url + "/")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    page.locator(".okf-studio-launcher").click()

    panel = page.locator("#okf-panel")
    assert panel.get_attribute("role") == "dialog"
    assert panel.get_attribute("aria-modal") == "true"
    assert panel.get_attribute("aria-labelledby") == "okf-panel-title"
    assert page.locator("#okf-panel-title").inner_text() == "Studio"

    page.keyboard.press("Escape")
    assert panel.is_hidden()
    assert page.evaluate("document.activeElement === document.querySelector('.okf-studio-launcher')")


_LAUNCHER_FOCUSED = (
    "() => document.activeElement instanceof HTMLElement "
    "&& document.activeElement.classList.contains('okf-studio-launcher')"
)


@pytest.mark.parametrize("path,width,height", [
    ("/", 1440, 900),
    ("/tables/orders", 1440, 900),
    ("/__graph", 1440, 900),
    ("/", 390, 844),
    ("/tables/orders", 390, 844),
    ("/__graph", 390, 844),
])
def test_escape_after_palette_opened_a_panel_restores_a_connected_control(
    page, server_url: str, path: str, width: int, height: int
) -> None:
    """F5 regression: a palette-activated panel must not strand focus on <body>.

    When the palette was opened while <body> held focus, its close pass
    re-focused <body>; the panel then captured that incidental origin, and
    Escape sent focus nowhere. Panel and palette state must still close and
    focus must land on a persistent, connected control (the Studio launcher).
    """
    page.set_viewport_size({"width": width, "height": height})
    page.goto(server_url + path)
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    assert page.evaluate("() => document.activeElement === document.body")

    page.keyboard.press("Control+k")
    option = page.get_by_role("option", name="Open Comments panel")
    expect(option).to_be_visible()
    option.click()

    panel = page.locator("#okf-panel")
    expect(panel).to_be_visible()
    assert page.evaluate("() => window.okfLoomStudio.state.openPanel") == "comments"

    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    assert page.evaluate("() => window.okfLoomStudio.state.openPanel") is None
    assert page.evaluate(_LAUNCHER_FOCUSED), (
        f"Escape after a palette-opened panel left focus on "
        f"{page.evaluate('document.activeElement && document.activeElement.tagName')} "
        f"instead of the Studio launcher"
    )


@pytest.mark.parametrize("path,width,height", [
    ("/tables/orders", 1440, 900),
    ("/__graph", 1440, 900),
    ("/tables/orders", 390, 844),
    ("/__graph", 390, 844),
])
def test_launcher_escape_round_trip_restores_the_launcher(
    page, server_url: str, path: str, width: int, height: int
) -> None:
    """F5 acceptance: launcher→Escape restores the launcher on doc and graph
    routes at desktop and mobile widths (the index/modal variants are covered
    by the two launcher tests above)."""
    page.set_viewport_size({"width": width, "height": height})
    page.goto(server_url + path)
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")

    launcher = page.locator(".okf-studio-launcher")
    launcher.click()
    panel = page.locator("#okf-panel")
    expect(panel).to_be_visible()

    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    assert page.evaluate("() => window.okfLoomStudio.state.openPanel") is None
    assert page.evaluate(_LAUNCHER_FOCUSED)


def test_concept_view_controls_are_local_and_preserve_url_state(page, server_url: str) -> None:
    page.goto(server_url + "/tables/orders?view=source&focus=1")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")

    viewbar = page.locator(".okf-concept-viewbar")
    assert viewbar.is_visible()
    assert viewbar.get_by_role("button", name="Source").get_attribute("aria-pressed") == "true"
    focus = viewbar.get_by_role("button", name="Focus view")
    assert focus.get_attribute("aria-pressed") == "true"

    focus.click()
    assert "view=source" in page.url
    assert "focus=" not in page.url


@pytest.mark.parametrize("width", [390, 720, 360, 1280, 1440])
def test_concept_shell_and_local_viewbar_follow_normal_flow_without_overflow(page, server_url: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    metrics = page.evaluate("""() => {
      const shell=document.querySelector('.okf-shell'),bar=document.querySelector('.okf-concept-viewbar');
      const sr=shell.getBoundingClientRect(),br=bar.getBoundingClientRect();
      const visible=el=>!!el&&el.offsetParent!==null;
      return {shellHeight:sr.height,shellBottom:sr.bottom,barTop:br.top,
        overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth+1,
        destinations:[...shell.querySelectorAll('[data-okf-shell-item]')].filter(visible).map(el=>el.dataset.okfShellItem),
        modes:[...bar.querySelectorAll('button')].filter(visible).map(el=>el.textContent.trim())};
    }""")
    # Ledger top bar: 50px (DESIGN.md nav-height), the view bar in flow below.
    assert metrics["shellHeight"] >= 50 and metrics["barTop"] >= metrics["shellBottom"] - 1, metrics
    assert metrics["overflow"] is False, metrics
    assert set(("search", "index", "graph", "studio", "appearance")).issubset(metrics["destinations"]), metrics
    assert {"Rendered", "Source", "Split", "Focus view"}.issubset(metrics["modes"]), metrics


def test_studio_home_preserves_session_review_command_and_authority_capabilities(page, server_url: str) -> None:
    page.goto(server_url + "/")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    page.locator(".okf-studio-launcher").click()

    panel = page.locator("#okf-panel")
    assert panel.get_by_role("button", name="Agent watching").count() == 1
    assert panel.get_by_role("button", name="Comments").count() == 1
    assert panel.get_by_role("button", name="Changes").count() == 1
    assert panel.get_by_role("button", name="Agent activity").count() == 1
    assert panel.get_by_role("button", name="Open the command palette").count() == 1
    assert panel.get_by_role("heading", name="Authority and sharing").count() == 1
    assert "Local editing authority is enabled" in panel.locator(".okf-studio-authority").inner_text()

    panel.get_by_role("button", name="Comments").click()
    assert page.locator("#okf-panel-title").inner_text() == "Comments"


def test_route_links_expose_current_state_and_focus_visible_targets(page, server_url: str) -> None:
    page.goto(server_url + "/")
    index_link = page.locator('[data-okf-shell-item="index"] a')
    assert index_link.get_attribute("aria-current") == "page"

    # An empty query opens the full search route; the search form itself then
    # carries the current-destination marker (there is no Search link).
    page.locator('.okf-shell__search-form input').press("Enter")
    page.wait_for_url(lambda url: '/__search' in url)
    search_form = page.locator('form[data-okf-shell-item="search"]')
    assert search_form.get_attribute("aria-current") == "page"
    assert page.locator('[data-okf-shell-item="index"] a').get_attribute("aria-current") is None
    assert page.evaluate("document.activeElement === document.querySelector('[data-okf-route-heading]')")

    page.locator('[data-okf-shell-item="index"] a').click()
    page.wait_for_url(server_url + "/")
    assert page.evaluate("document.activeElement === document.querySelector('[data-okf-route-heading]')")

    page.locator('[data-okf-shell-item="graph"] a').click()
    page.wait_for_url(server_url + "/__graph")
    heading = page.locator(".okf-graph-route-title")
    assert heading.is_visible()
    graph_focus = page.evaluate("() => ({matches:document.activeElement === document.querySelector('.okf-graph-route-title'), active:document.activeElement && document.activeElement.outerHTML})")
    assert graph_focus["matches"], graph_focus
    assert heading.evaluate("el => getComputedStyle(el).outlineStyle !== 'none'")

    page.goto(server_url + "/tables/orders")
    assert page.locator('[data-okf-shell-item="index"] a').get_attribute("aria-current") == "page"
    assert page.locator(".okf-page__title").is_visible()


def test_route_navigation_degrades_normally_when_session_storage_is_denied(server_url: str) -> None:
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": 600, "height": 800})
        context.add_init_script(
            "Object.defineProperty(window, 'sessionStorage', {get(){throw new DOMException('denied')}})"
        )
        page = context.new_page()
        try:
            page.goto(server_url + "/")
            page.locator('[data-okf-shell-item="graph"] a').click()
            page.wait_for_url(server_url + "/__graph")
            assert page.locator('[data-okf-shell-item="graph"] a').get_attribute("aria-current") == "page"
            heading = page.locator(".okf-graph-route-title")
            expect(heading).to_be_focused()
            assert page.evaluate("window.name") == ""

            page.locator("#okf-search").focus()
            page.locator('[data-okf-shell-item="index"] a').click()
            page.wait_for_url(server_url + "/")
            page.go_back()
            page.wait_for_url(server_url + "/__graph")
            expect(heading).not_to_be_focused()

            page.reload()
            expect(heading).not_to_be_focused()

            page.goto(server_url + "/")
            page.evaluate("""() => {
              const link=document.createElement('a');
              link.id='ordinary-graph-entry'; link.href='/__graph'; link.textContent='Ordinary graph entry';
              document.querySelector('main').prepend(link);
            }""")
            page.locator("#ordinary-graph-entry").click()
            page.wait_for_url(server_url + "/__graph")
            expect(heading).not_to_be_focused()

            direct = context.new_page()
            direct.goto(server_url + "/__graph")
            expect(direct.locator(".okf-graph-route-title")).not_to_be_focused()
            direct.close()
        finally:
            context.close()
            browser.close()


def test_graph_mobile_keeps_primary_nav_connected_visible_and_unclipped(page, server_url: str) -> None:
    page.set_viewport_size({"width": 600, "height": 800})
    page.goto(server_url + "/__graph")
    nav = page.get_by_role("navigation", name="Primary navigation")
    assert nav.is_visible()
    assert nav.evaluate("el => el.parentElement.matches('[data-okf-application-shell]')")
    assert page.locator(".okf-mobile-secondary .okf-shell__nav").count() == 0
    for name in ("Index", "Graph"):
        assert nav.get_by_role("link", name=name).is_visible()
    searchbox = nav.get_by_role("searchbox", name="Search this bundle")
    submit = nav.locator('.okf-shell__search-submit')
    assert searchbox.is_visible()
    assert submit.is_visible() and submit.inner_text().strip() == "Search"
    assert nav.get_by_role("link", name="Search").count() == 0
    clipped = page.evaluate(
        "[...document.querySelectorAll('.okf-shell a,.okf-shell button,.okf-shell input')].filter(el=>el.offsetParent!==null).map(el=>{const r=el.getBoundingClientRect();return {name:el.getAttribute('aria-label')||el.textContent.trim(),left:r.left,right:r.right}}).filter(row=>row.left<0||row.right>innerWidth+.5)"
    )
    assert clipped == [], clipped
    tour = page.get_by_role("dialog", name="Graph tour")
    if tour.is_visible():
        tour.get_by_role("button", name="Skip").click()
    # Coherent keyboard order: the field hands focus to its own labelled
    # submit, then to the primary destinations.
    searchbox.focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.matches('.okf-shell__search-submit')")
    current_item = "document.activeElement.closest('[data-okf-shell-item]')?.dataset.okfShellItem"
    page.keyboard.press("Tab")
    assert page.evaluate(current_item) == "index"
    page.keyboard.press("Tab")
    assert page.evaluate(current_item) == "graph"


def test_graph_nojs_keeps_honest_search_index_and_graph_destinations(server_url: str) -> None:
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(java_script_enabled=False, viewport={"width": 600, "height": 800})
        page = context.new_page()
        try:
            page.goto(server_url + "/__graph")
            nav = page.get_by_role("navigation", name="Primary navigation")
            for name in ("Index", "Graph"):
                assert nav.get_by_role("link", name=name).is_visible()
            assert nav.get_by_role("link", name="Search").count() == 0
            assert nav.get_by_role("searchbox", name="Search this bundle").is_visible()
            assert nav.get_by_role("button", name="Search").is_visible()
            assert nav.get_by_role("link", name="Graph").get_attribute("aria-current") == "page"
            assert page.locator(".okf-mobile-secondary").count() == 0
            # The real GET form navigates without JavaScript and lands on the
            # honest full search route with server-rendered results.
            searchbox = nav.get_by_role("searchbox", name="Search this bundle")
            searchbox.fill("orders")
            nav.get_by_role("button", name="Search").click()
            page.wait_for_url(lambda url: "/__search" in url and "q=orders" in url)
            assert page.locator(".okf-search-result").count() >= 1
        finally:
            context.close()
            browser.close()


def test_public_panel_extension_activates_returns_and_restores_focus(page, server_url: str) -> None:
    page.goto(server_url + "/")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.register")
    launcher = page.locator(".okf-studio-launcher")
    launcher.click()
    page.evaluate(
        """window.okfLoomStudio.register('panel', {
          id:'coverage-proof', label:'Coverage proof',
          render(container){ container.textContent='Extension active'; }
        })"""
    )
    extension = page.get_by_role("button", name="Coverage proof")
    assert extension.is_visible()
    extension.click()
    assert page.evaluate("window.okfLoomStudio.panels['coverage-proof']._btn.getAttribute('aria-expanded')") == "true"
    assert page.locator("#okf-panel-body").inner_text() == "Extension active"
    back = page.get_by_role("button", name="Back to Studio")
    assert back.is_visible()
    back.click()
    assert page.locator("#okf-panel-title").inner_text() == "Studio"
    expect(extension).to_be_focused()
    page.get_by_role("button", name="Close panel").click()
    expect(launcher).to_be_focused()


def test_studio_attention_is_actionable_unread_and_reconnect_honest(page, server_url: str) -> None:
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.eventsReady")
    launcher = page.locator(".okf-studio-launcher")
    assert launcher.get_attribute("data-attention") == "idle"

    page.evaluate("window.okfLoomLive.emit('activity',{event_id:'shell-new-work',type:'activity',action:'update',ts:new Date().toISOString()})")
    assert launcher.get_attribute("data-attention") == "pending"
    expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+1 change"))

    launcher.click()
    page.get_by_role("button", name="Changes").click()
    assert launcher.get_attribute("data-attention") == "idle"
    page.get_by_role("button", name="Close panel").click()

    page.evaluate("window.okfLoomLive.emit('conn',{state:'reconnecting'})")
    # Truthful classification (phase-1 status fix): a degraded-but-recovering
    # stream is NOT a failure — the name still says "Reconnecting", but no
    # "· Error" projection fires. Only a dead connection is a failure.
    assert launcher.get_attribute("data-attention") == "idle"
    expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Reconnecting"))
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.okf-studio-launcher'), '::after').content"
    ) == "none"
    page.evaluate("window.okfLoomLive.emit('conn',{state:'online'})")
    assert launcher.get_attribute("data-attention") == "idle"

    page.evaluate("window.okfLoomLive.emit('conn',{state:'offline'})")
    assert launcher.get_attribute("data-attention") == "failure"
    expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Offline"))
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.okf-studio-launcher'), '::after').content"
    ).replace('"', "") .strip() == "· Error"
    page.evaluate("window.okfLoomLive.emit('conn',{state:'online'})")
    assert launcher.get_attribute("data-attention") == "idle"

    page.evaluate("window.okfLoomLive.emit('activity',{event_id:'stale-after-resync',type:'activity',action:'update'})")
    assert launcher.get_attribute("data-attention") == "pending"
    resync = page.evaluate("""async () => {
      await Promise.all(window.okfLoomLive.emit('resync',{rev:1}));
      return {
        attention: document.querySelector('.okf-studio-launcher').dataset.attention,
        unread: window.okfLoomStudio.state.attention.unread.size,
        unreadKeys: [...window.okfLoomStudio.state.attention.unread],
        eventKeys: window.okfLoomStudio.state.events.map(e => e.event_id ?? e.event_cursor ?? null).slice(0,5)
      };
    }""")
    assert resync["attention"] == "idle" and resync["unread"] == 0, resync


_MASTHEAD_GEOMETRY = """() => {
  const box = selector => document.querySelector(selector).getBoundingClientRect();
  const shellNode = document.querySelector('.okf-shell');
  const shell = box('.okf-shell'), brand = box('.okf-shell__brand'), nav = box('.okf-shell__nav');
  const items = [...document.querySelectorAll('.okf-shell__nav > *')].map(node => node.getBoundingClientRect());
  return {
    rail: document.body.classList.contains('okf-panel-rail'),
    shellWidth: shell.width, shellRight: shell.right, navRight: nav.right,
    sharesRow: brand.top < nav.bottom && nav.top < brand.bottom,
    itemRows: new Set(items.map(r => Math.round((r.top + r.bottom) / 16))).size,
    overflow: shellNode.scrollWidth - shellNode.clientWidth,
    launcher: box('.okf-studio-launcher').width,
    searchLeft: box('.okf-shell__search-form').left,
    graphLeft: box('[data-okf-shell-item="graph"]').left,
  };
}"""


def _assert_single_row_masthead(geometry: dict) -> None:
    assert geometry["sharesRow"], geometry
    assert geometry["itemRows"] == 1, geometry
    assert geometry["overflow"] <= 0, geometry
    assert geometry["navRight"] <= geometry["shellRight"] + 0.5, geometry


def test_masthead_stays_one_row_beside_the_studio_rail_and_counts_do_not_move_it(
    page, server_url: str,
) -> None:
    """At 1200 the 320px Comments rail leaves the masthead ~880px while every
    viewport query still reads desktop; the former viewport guess wrapped it
    into two rows. The masthead now sizes from its real width, and the
    launcher keeps one width while unread counts arrive, so the destinations
    beside it never jump while an agent works."""
    failures = _install_error_probe(page)
    page.set_viewport_size({"width": 1200, "height": 800})
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.eventsReady")
    launcher = page.locator(".okf-studio-launcher")
    panel = page.locator("#okf-panel")

    launcher.click()
    panel.get_by_role("button", name="Comments").click()
    page.wait_for_function("() => document.body.classList.contains('okf-panel-rail')")
    rail_idle = page.evaluate(_MASTHEAD_GEOMETRY)
    assert rail_idle["shellWidth"] < 900, rail_idle
    _assert_single_row_masthead(rail_idle)
    # The consolidated label is visual only: the name still reports status.
    expect(launcher).to_have_accessible_name(re.compile(r"^Studio\s+·\s+Editable$"))

    page.evaluate("window.okfLoomLive.emit('activity',{event_id:'masthead-1',type:'activity',action:'update',ts:new Date().toISOString()})")
    expect(launcher).to_have_attribute("data-attention", "pending")
    # The compact pending marker's alternative text keeps the phrase.
    expect(launcher).to_have_accessible_name(re.compile(r"^Studio\s+·\s+1 change\s+·\s+Needs attention$"))
    page.evaluate("window.okfLoomLive.emit('activity',{event_id:'masthead-2',type:'activity',action:'update',ts:new Date().toISOString()})")
    expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+2 changes"))
    rail_pending = page.evaluate(_MASTHEAD_GEOMETRY)
    _assert_single_row_masthead(rail_pending)
    for key in ("launcher", "searchLeft", "graphLeft"):
        assert rail_pending[key] == rail_idle[key], (key, rail_idle, rail_pending)
    _capture_evidence(page, "desktop-1200-rail-masthead-pending.png")

    # Full-width masthead: the status is spelled out, in a reserved slot.
    page.get_by_role("button", name="Close panel").click()
    page.wait_for_function("() => !document.body.classList.contains('okf-panel-rail')")
    full_pending = page.evaluate(_MASTHEAD_GEOMETRY)
    _assert_single_row_masthead(full_pending)
    expect(launcher).to_contain_text("2 changes")
    launcher.click()
    panel.get_by_role("button", name="Changes").click()
    expect(launcher).to_have_attribute("data-attention", "idle")
    page.get_by_role("button", name="Close panel").click()
    page.wait_for_function("() => !document.body.classList.contains('okf-panel-rail')")
    full_idle = page.evaluate(_MASTHEAD_GEOMETRY)
    _assert_single_row_masthead(full_idle)
    for key in ("launcher", "searchLeft", "graphLeft"):
        assert full_idle[key] == full_pending[key], (key, full_pending, full_idle)
    assert failures == []


# Records every text the launcher summary ever shows, from the moment Studio
# creates it, with page timestamps.
_LAUNCHER_TEXT_RECORDER = """(() => {
  const log = [];
  window.__okfLauncherLog = log;
  const attach = () => {
    const summary = document.querySelector('.okf-shell__studio-slot:not([hidden]) .okf-studio-launcher__summary');
    if (!summary) return false;
    const record = () => log.push({t: performance.now(), text: summary.textContent});
    record();
    new MutationObserver(record).observe(summary, {childList: true, characterData: true, subtree: true});
    return true;
  };
  const pending = new MutationObserver(() => { if (attach()) pending.disconnect(); });
  pending.observe(document, {childList: true, subtree: true});
})();"""


def test_launcher_does_not_flash_connecting_during_a_healthy_handshake(page, server_url: str) -> None:
    """Every fresh load used to print "Studio · Connecting…" for the few
    hundred milliseconds the SSE handshake takes. The launcher now names the
    session's authority from the start and only reports a connecting or
    reconnecting stream that outlasts a one-second grace; the in-panel chip
    stays immediate, and a dead stream is never delayed."""
    page.add_init_script(_LAUNCHER_TEXT_RECORDER)
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfLoomLive && window.okfLoomLive.state === 'online'")
    page.wait_for_timeout(1200)
    log = page.evaluate("window.__okfLauncherLog")
    assert log and log[0]["text"] == " · Editable", log
    created = log[0]["t"]
    # Only a stream that is still not up after the grace may be announced.
    early = [entry for entry in log if "onnecting" in entry["text"] and entry["t"] - created < 950]
    assert early == [], log
    assert log[-1]["text"] == " · Editable", log

    # A brief reconnect blip never reaches the launcher ...
    page.evaluate("window.__okfLauncherLog.length = 0")
    page.evaluate("""() => { window.okfLoomLive.emit('conn', {state: 'reconnecting'});
      setTimeout(() => window.okfLoomLive.emit('conn', {state: 'online'}), 150); }""")
    page.wait_for_timeout(1300)
    assert all("Reconnecting" not in entry["text"] for entry in page.evaluate("window.__okfLauncherLog"))

    # ... while a sustained one is reported after the grace, not before, and
    # the Studio home connection chip reports it immediately.
    launcher = page.locator(".okf-studio-launcher")
    started = page.evaluate("() => { window.okfLoomLive.emit('conn', {state: 'reconnecting'}); return performance.now(); }")
    expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Reconnecting"))
    shown = next(entry["t"] for entry in page.evaluate("window.__okfLauncherLog") if "Reconnecting" in entry["text"])
    assert shown - started >= 950, (started, shown)
    launcher.click()
    expect(page.locator("#okf-panel .okf-conn")).to_have_attribute("data-state", "reconnecting")
    page.evaluate("window.okfLoomLive.emit('conn', {state: 'offline'})")
    expect(launcher).to_have_attribute("data-attention", "failure")
    expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Offline"))


def test_connection_chip_distinguishes_boot_connecting_from_reconnecting(
    page, server_url: str,
) -> None:
    """Phase-1 status truthfulness: the chip state machine never collapses
    the first-boot "connecting" phase into "reconnecting" (which, paired
    with the old failure classification, made every healthy fresh load
    announce 'Reconnecting · Error'), and neither degraded-but-recovering
    state raises the failure projection — only 'offline' does.
    """
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.eventsReady")
    launcher = page.locator(".okf-studio-launcher")
    # The connection chip is part of the Studio home panel's session UI; it
    # only exists in the DOM while that panel is rendered. Scope to the
    # panel: concept articles carry unrelated `.okf-conn` connection cards.
    launcher.click()
    chip = page.locator("#okf-panel .okf-conn")
    chip.wait_for(state="attached")

    def chip_state() -> str:
        return page.evaluate(
            "() => { const chip = document.querySelector('#okf-panel .okf-conn');"
            " return chip ? chip.dataset.state : null; }"
        )

    page.evaluate("window.okfLoomStudio._applyConnectionState('connecting')")
    assert chip_state() == "connecting"
    assert launcher.get_attribute("data-attention") == "idle"
    expect(launcher).to_have_accessible_name(re.compile(r"Connecting"))

    page.evaluate("window.okfLoomStudio._applyConnectionState('reconnecting')")
    assert chip_state() == "reconnecting"
    assert launcher.get_attribute("data-attention") == "idle"
    expect(launcher).to_have_accessible_name(re.compile(r"Reconnecting"))

    page.evaluate("window.okfLoomStudio._applyConnectionState('offline')")
    assert chip_state() == "offline"
    assert launcher.get_attribute("data-attention") == "failure"

    page.evaluate("window.okfLoomStudio._applyConnectionState('online')")
    assert chip_state() == "online"
    assert launcher.get_attribute("data-attention") == "idle"
    expect(launcher).to_have_accessible_name(re.compile(r"Studio"))


def _genuine_ready_frame(server_url: str) -> tuple[str, str]:
    """Capture the real server's healthy /__events connect bytes.

    Reads only up to the first frame terminator (the ready control frame);
    the stream itself never ends, so a naive bulk read would block forever.
    """
    with urllib.request.urlopen(server_url + "/__events", timeout=5) as stream:
        content_type = stream.headers.get("Content-Type", "text/event-stream")
        chunks = bytearray()
        while b"\n\n" not in chunks and len(chunks) < 8192:
            byte = stream.read(1)
            if not byte:
                break
            chunks += byte
    body = chunks.decode("utf-8", "replace")
    assert "event: ready" in body, body[:200]
    # A short native retry keeps the real reconnect deterministic; the retry
    # directive is the server's own SSE reconnection-time knob.
    return content_type, body.rstrip("\n") + "\nretry: 100\n\n"


def _hold_first_two_events(held: dict):
    """Hold the initial /__events attempt AND the first genuine retry, so both
    the "connecting" and the "reconnecting" phases are durable, observable
    states instead of transient windows. Later attempts pass through."""
    def hold(route) -> None:
        if "route" not in held:
            held["route"] = route
        elif "retry" not in held:
            held["retry"] = route
        else:
            route.continue_()
    return hold


def _release_held_events(page, held: dict, key: str, content_type: str, ready_body: str) -> None:
    """Answer a held attempt with the server's genuine ready frame. The key
    stays in `held` so the hold handler still files the next attempt (the
    genuine retry) under its own slot instead of reusing this one."""
    held[key].fulfill(status=200, headers={
        "Content-Type": content_type, "Cache-Control": "no-store"},
        body=ready_body)
    held[key + "_served"] = True


def _wait_for_held_retry(page, held: dict, timeout: float = 5.0) -> None:
    """The native retry fires ~100ms after the error; wait for its route.
    Playwright dispatches route callbacks while its own calls pump, so the
    bounded wait uses page.wait_for_timeout rather than time.sleep."""
    deadline = time.monotonic() + timeout
    while "retry" not in held:
        if time.monotonic() > deadline:
            raise AssertionError("the genuine /__events retry never arrived")
        page.wait_for_timeout(50)


def test_connection_owner_lifecycle_connecting_online_reconnecting_online(
    page, server_url: str,
) -> None:
    """The real live.js producer drives the honest connection lifecycle.

    Regression for the phase-1 review blocker: boot() used to announce
    "reconnecting" on the FIRST /__events attempt, before any error. The
    session's first pending attempt must read "connecting" (a held healthy
    response proves it), flip to online on the genuine open/ready frames,
    become "reconnecting" only after a real stream error, and recover to
    online through the real reconnect. The unsupported-EventSource path stays
    honestly offline. Proof never injects _applyConnectionState; that
    synthetic projection check remains as a separate supporting test above.
    """
    content_type, ready_body = _genuine_ready_frame(server_url)
    held: dict = {}
    page.route("**/__events", _hold_first_two_events(held))
    try:
        page.goto(server_url + "/tables/orders")
        # The very first pending attempt must BE "connecting"; the pre-fix
        # producer flipped to "reconnecting" synchronously during boot, so a
        # regression fails here within seconds instead of at the assertions.
        page.wait_for_function("() => window.okfLoomLive && window.okfLoomLive.state === 'connecting'", timeout=5_000)
        launcher = page.locator(".okf-studio-launcher")
        assert page.evaluate("window.okfLoomLive._debug.connState") == "connecting"
        assert launcher.get_attribute("data-attention") == "idle"
        expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Connecting"))
        assert page.evaluate(
            "getComputedStyle(document.querySelector('.okf-studio-launcher'), '::after').content"
        ) == "none"
        launcher.click()
        chip = page.locator("#okf-panel .okf-conn")
        expect(chip).to_have_attribute("data-state", "connecting")
        expect(chip).to_have_accessible_name("Live updates connection: connecting")
        # R3: Studio home's status line is the connection only. It never
        # says "Live" for permission and carries no counts; updateBadges()
        # keeps it on the owner's state through every transition below.
        home_status = page.locator("#okf-panel .okf-studio-home__status")
        expect(home_status).to_have_text("Live updates: connecting\u2026")
        # Record the owner's own published transitions (public hub) so the
        # ordered lifecycle is provable without racing transient windows.
        page.evaluate("""() => {
          window.__connSeq = [];
          window.okfLoomLive.on('conn', (d) => window.__connSeq.push(d && d.state));
        }""")

        # Release the held attempt: the real transport opens and the server's
        # genuine ready frame drives online through the real handler path;
        # the stream then ends, the real error fires, and the genuine native
        # retry stays held below — "reconnecting" is durable while it waits.
        _release_held_events(page, held, "route", content_type, ready_body)
        # Ordered owner truth: online comes from the genuine open/ready, and
        # reconnecting only after the real stream end fired a real error.
        # (onopen and the ready frame both legitimately publish "online".)
        page.wait_for_function(
            "() => { const s = window.__connSeq || [];"
            " return s.length >= 2 && s[0] === 'online' && s[s.length-1] === 'reconnecting'"
            " && s.every(x => x === 'online' || x === 'reconnecting'); }",
            timeout=15_000)
        _wait_for_held_retry(page, held)
        assert launcher.get_attribute("data-attention") == "idle"
        expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Reconnecting"))
        expect(chip).to_have_attribute("data-state", "reconnecting")
        expect(home_status).to_have_text("Live updates: reconnecting\u2026")

        # Answer the genuine retry with the server's genuine ready frame,
        # then unroute so any later retry reaches the real server.
        _release_held_events(page, held, "retry", content_type, ready_body)
        page.unroute("**/__events")
        page.wait_for_function(
            "() => { const s = window.__connSeq || [];"
            " return window.okfLoomLive.state === 'online' && s.length >= 3"
            " && s[0] === 'online' && s[s.length-1] === 'online'"
            " && s.every(x => x === 'online' || x === 'reconnecting'); }",
            timeout=15_000)
        assert launcher.get_attribute("data-attention") == "idle"
        # R3/D4: the launcher names the permission ("Editable"); "Live" is
        # the connection chip's word for connected.
        expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Editable\s*$"))
        expect(chip).to_have_attribute("data-state", "online")
        expect(home_status).to_have_text("Live updates: connected")

        # Unsupported transport stays honestly offline (producer-owned), and
        # recovery from offline re-enters through connecting — not by
        # pretending the recovery already succeeded.
        page.evaluate("""() => {
          const Real = window.EventSource;
          window.EventSource = function () { throw new TypeError('unsupported'); };
          try { window.okfLoomLive._connectForTest(); } finally { window.EventSource = Real; }
        }""")
        assert page.evaluate("window.okfLoomLive.state") == "offline"
        assert launcher.get_attribute("data-attention") == "failure"
        expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Offline"))
        expect(home_status).to_have_text("Live updates: offline")
        page.evaluate("() => window.okfLoomLive._connectForTest()")
        page.wait_for_function("() => window.okfLoomLive.state === 'online'", timeout=15_000)
        assert launcher.get_attribute("data-attention") == "idle"
        expect(home_status).to_have_text("Live updates: connected")
    finally:
        page.unroute("**/__events")


@pytest.fixture(scope="module")
def readonly_url(tmp_path_factory) -> str:
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("shell-readonly") / "bundle")
    with live_server(bundle, "--no-edit") as base:
        yield base


def test_connection_status_for_visitor_without_privileges_stays_honest(
    page, readonly_url: str,
) -> None:
    """Public, unprivileged visitor through the same real lifecycle.

    With no edit or comment authority, every degraded state reads
    "Read-only · <state>" with the idle (never failure) projection except a
    genuinely dead connection, and the comments surface confirms no
    commenting privilege was granted while the connection was still only
    "connecting".
    """
    content_type, ready_body = _genuine_ready_frame(readonly_url)
    held: dict = {}
    page.route("**/__events", _hold_first_two_events(held))
    try:
        page.goto(readonly_url + "/tables/orders")
        page.wait_for_function("() => window.okfLoomLive && window.okfLoomLive.state === 'connecting'", timeout=5_000)
        launcher = page.locator(".okf-studio-launcher")
        assert launcher.get_attribute("data-attention") == "idle"
        expect(launcher).to_have_accessible_name(
            re.compile(r"Studio\s+·\s+Read-only\s+·\s+Connecting"))
        # No privilege appears while the stream is merely pending: the
        # comments pane states the disabled capability instead of offering it.
        launcher.click()
        page.locator("#okf-panel").get_by_role("button", name="Comments").click()
        expect(page.get_by_text("Commenting is disabled", exact=False)).to_be_visible()

        _release_held_events(page, held, "route", content_type, ready_body)
        page.wait_for_function("() => window.okfLoomLive.state === 'reconnecting'", timeout=15_000)
        _wait_for_held_retry(page, held)
        assert launcher.get_attribute("data-attention") == "idle"
        expect(launcher).to_have_accessible_name(
            re.compile(r"Studio\s+·\s+Read-only\s+·\s+Reconnecting"))

        _release_held_events(page, held, "retry", content_type, ready_body)
        page.unroute("**/__events")
        page.wait_for_function("() => window.okfLoomLive.state === 'online'", timeout=15_000)
        expect(launcher).to_have_accessible_name(re.compile(r"Studio\s+·\s+Read-only\s*$"))

        page.evaluate("""() => {
          const Real = window.EventSource;
          window.EventSource = function () { throw new TypeError('unsupported'); };
          try { window.okfLoomLive._connectForTest(); } finally { window.EventSource = Real; }
        }""")
        assert page.evaluate("window.okfLoomLive.state") == "offline"
        assert launcher.get_attribute("data-attention") == "failure"
        expect(launcher).to_have_accessible_name(
            re.compile(r"Studio\s+·\s+Read-only\s+·\s+Offline"))
    finally:
        page.unroute("**/__events")


@pytest.mark.parametrize("width, max_rows, max_shell_h", [(390, 2, 150), (320, 3, 190)])
def test_mobile_shell_compacts_rows_with_honest_identity_and_touch_targets(
    page, server_url: str, width: int, max_rows: int, max_shell_h: int,
) -> None:
    """Phase-1 mobile chrome compaction on the real concept route.

    The former four stacked rows (~195px of persistent chrome at 390px)
    must wrap into the compact two-row arrangement (three rows at 320px,
    where brand + Appearance exceed the linear budget) while keeping every
    destination, a usable search field with its labelled submit, the full
    honest brand name (wrapping, never an ellipsis cut or a shatter into a
    narrow column), >=40px touch height on controls, and zero overflow.
    """
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    metrics = page.evaluate("""() => {
      const shell = document.querySelector('[data-okf-application-shell], .okf-shell');
      const items = [...shell.querySelectorAll('[data-okf-shell-item]')].filter(el => el.offsetParent !== null);
      const boxes = items.map(el => { const r = el.getBoundingClientRect();
        return {item: el.dataset.okfShellItem, y: r.top, x: r.left, w: r.width, h: r.height}; });
      // Cluster into visual rows with an 8px tolerance (input vs button
      // baseline rounding is ~1px).
      const ys = boxes.map(b => b.y).sort((a, b) => a - b);
      let rows = ys.length ? 1 : 0;
      for (let i = 1; i < ys.length; i++) if (ys[i] - ys[i - 1] > 8) rows++;
      const strong = shell.querySelector('.okf-topbar__brand strong');
      const brand = strong ? {text: strong.textContent.trim(),
        clipped: strong.scrollWidth > strong.clientWidth + 1,
        height: strong.getBoundingClientRect().height} : null;
      const submit = shell.querySelector('.okf-shell__search-submit');
      const appearance = shell.querySelector('.okf-appearance-trigger');
      const smallControls = boxes.filter(b => b.item !== 'bundle' && b.h < 40).map(b => b.item);
      return {shellH: shell.getBoundingClientRect().height, rows, boxes, brand,
        submitText: submit ? submit.textContent.trim() : null,
        appearanceText: appearance ? appearance.textContent.trim() : null,
        smallControls,
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1};
    }""")
    assert metrics["overflow"] is False, metrics
    assert metrics["shellH"] <= max_shell_h, metrics
    assert metrics["rows"] <= max_rows, metrics
    assert metrics["brand"] is not None, metrics
    # Honest identity: the full authored bundle name is present, never
    # clipped (no ellipsis cut, no shatter into a narrow column), and stays
    # within the compact budget (wrap to 2 lines at most at 390px).
    assert metrics["brand"]["text"] != "" and metrics["brand"]["clipped"] is False, metrics
    assert metrics["brand"]["height"] <= 60, metrics
    assert metrics["submitText"] == "Search", metrics
    assert metrics["appearanceText"] and metrics["appearanceText"].startswith("Appearance"), metrics
    assert metrics["smallControls"] == [], metrics
    assert metrics["brand"]["height"] >= 24, metrics  # WCAG 2.5.8 floor for the text link
    assert set(("search", "index", "graph", "studio", "appearance")).issubset(
        {b["item"] for b in metrics["boxes"]}), metrics


def test_search_filters_disclosure_is_expanding_and_marked(page, server_url: str) -> None:
    """Phase-1 clarity: the working native filters <details> must read as a
    disclosure — a visible non-colour affordance (rotating chevron glyph)
    replacing the marker the flex summary suppresses — and must really
    expand and collapse its filter grid through the summary.

    U8: with no active filter the summary reads as a plain "Filters"
    disclosure — the redundant zero count is omitted from the rendered text
    and the accessible name. A nonzero count stays meaningful ("Filters, N
    active") and the native disclosure/keyboard behaviour never regresses.
    """
    # Zero state: no number is spent on nothing, and the disclosure is closed.
    page.goto(server_url + "/__search")
    page.locator(".okf-search-workbench").wait_for(state="visible")
    disclosure = page.locator("details.okf-search-filters")
    expect(disclosure).to_have_count(1)
    summary = disclosure.locator("summary")
    assert summary.inner_text().strip() == "Filters", summary.inner_text()
    assert "0 active" not in summary.text_content()
    # The accessible name must not announce a zero count either (the trailing
    # whitespace/glyph allowance keeps this a name assertion, not a CSS one).
    expect(summary).to_have_accessible_name(re.compile(r"^Filters\s*▾?\s*$"))

    grid = disclosure.locator(".okf-search-filter-grid")
    assert not disclosure.evaluate("el => el.open")
    expect(grid).to_be_hidden()
    summary.click()
    assert disclosure.evaluate("el => el.open")
    expect(grid).to_be_visible()
    summary.click()
    assert not disclosure.evaluate("el => el.open")
    expect(grid).to_be_hidden()

    def chevron_state() -> dict:
        return page.evaluate("""() => {
          const s = document.querySelector('details.okf-search-filters > summary');
          const st = getComputedStyle(s, '::after');
          return {content: st.content, rotation: st.transform};
        }""")

    summary.click()
    opened = chevron_state()
    assert opened["content"] not in ("none", "normal"), opened  # glyph is present
    summary.click()
    closed = chevron_state()
    assert closed["content"] == opened["content"] and closed["rotation"] != opened["rotation"], (closed, opened)

    # Nonzero state: the count is meaningful and the disclosure auto-opens.
    page.goto(server_url + "/__search?type=Service")
    page.locator(".okf-search-workbench").wait_for(state="visible")
    summary = page.locator("details.okf-search-filters > summary")
    assert summary.inner_text().strip() == "Filters, 1 active", summary.inner_text()
    expect(summary).to_have_accessible_name(re.compile(r"^Filters, 1 active\b"))
    assert page.locator("details.okf-search-filters").evaluate("el => el.open") is True

    # Clearing returns to the honest zero state.
    page.locator(".okf-search-filter-actions").get_by_role("link", name="Clear").click()
    page.locator(".okf-search-workbench").wait_for(state="visible")
    summary = page.locator("details.okf-search-filters > summary")
    assert summary.inner_text().strip() == "Filters", summary.inner_text()
    assert "0 active" not in summary.text_content()
    assert not page.locator("details.okf-search-filters").evaluate("el => el.open")


def test_escape_ownership_differs_between_modal_and_nonmodal_editables(page, server_url: str) -> None:
    page.set_viewport_size({"width": 600, "height": 800})
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.commentsReady")
    page.locator(".okf-studio-launcher").click()
    page.locator("#okf-panel").get_by_role("button", name="Comments").click()
    composer = page.locator("#okf-panel").get_by_role("textbox", name="Comment for agent")
    composer.focus()
    page.keyboard.press("Escape")
    assert page.locator("#okf-panel").is_hidden()
    expect(page.locator(".okf-studio-launcher")).to_be_focused()

    page.set_viewport_size({"width": 1440, "height": 900})
    page.locator(".okf-studio-launcher").click()
    composer = page.locator("#okf-panel").get_by_role("textbox", name="Comment for agent")
    if not composer.is_visible():
        page.locator("#okf-panel").get_by_role("button", name="Comments").click()
    composer.focus()
    page.keyboard.press("Escape")
    assert page.locator("#okf-panel").is_visible()


@pytest.mark.parametrize("width", [390, 600, 640, 641])
def test_graph_shell_boundary_geometry_and_rendered_pixels(page, server_url: str, tmp_path: Path, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1")
    metrics = page.evaluate("""() => {
      const rect = el => el && el.getBoundingClientRect().toJSON();
      const visible = el => !!el && el.offsetParent !== null;
      const shell = document.querySelector('[data-okf-application-shell]');
      const brand = shell.querySelector('.okf-shell__brand');
      const nav = shell.querySelector('.okf-shell__nav');
      const workspace = document.querySelector('.okf-layout');
      const pane = document.querySelector('.okf-graph-mobile-nav');
      const more = document.querySelector('.okf-mobile-secondary__toggle');
      const clipped = [...shell.querySelectorAll('a,button,input')].filter(visible).filter(el => {
        const r=el.getBoundingClientRect(); return r.left < -.5 || r.right > innerWidth + .5;
      }).map(el => el.getAttribute('aria-label') || el.textContent.trim());
      return {shell:rect(shell),brand:rect(brand),nav:rect(nav),workspace:rect(workspace),pane:rect(pane),more:rect(more),
        clipped,overflow:document.documentElement.scrollWidth > innerWidth + 1,
        narrow:document.body.hasAttribute('data-okf-narrow-reflow'),
        destinations:[...nav.querySelectorAll('[data-okf-shell-item]')].filter(visible).map(el=>el.dataset.okfShellItem)};
    }""")
    assert not metrics["overflow"] and metrics["clipped"] == [], metrics
    assert metrics["brand"]["width"] >= 120 and metrics["brand"]["height"] >= 32, metrics
    assert metrics["workspace"]["width"] <= width + 1 and metrics["workspace"]["height"] >= 300, metrics
    assert set(("search", "index", "graph", "studio", "appearance")).issubset(metrics["destinations"]), metrics
    assert metrics["narrow"] is (width <= 640)
    if width <= 640:
        assert metrics["pane"], metrics
        assert metrics["pane"]["top"] >= metrics["nav"]["bottom"] - 1, metrics
        expect(page.get_by_role('searchbox', name='Search graph concepts')).to_be_visible()
    shot = tmp_path / f"graph-shell-{width}.png"
    page.screenshot(path=str(shot), full_page=False)
    assert shot.stat().st_size > 10_000


def test_mobile_graph_keeps_global_studio_ahead_of_graph_owned_rows(page, server_url: str) -> None:
    """U1: at phone width the global Studio control sits in the shared compact
    nav ahead of the graph route's own pane tabs — visually and in keyboard
    (DOM) order — with exactly one launcher in the same local-edit state.

    Every assertion is a real bounding box or live Cytoscape camera value on
    the graph route: pane tabs keep their 44px touch height, the canvas keeps
    real geometry, and a pane round-trip preserves the user camera (graph.js
    stays the single resize/pane owner — no second geometry writer exists)."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1")
    metrics = page.evaluate("""() => {
      const visible = el => !!el && el.getClientRects().length > 0;
      const rect = el => { const r = el.getBoundingClientRect();
        return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height}; };
      const launchers = [...document.querySelectorAll('.okf-studio-launcher')];
      const launcher = launchers.find(visible) || null;
      const paneNav = document.querySelector('.okf-graph-mobile-nav');
      const tabs = [...document.querySelectorAll('[data-graph-pane]')].filter(visible);
      const shell = document.querySelector('[data-okf-application-shell]');
      const workspace = document.querySelector('.okf-layout');
      // Keyboard order is DOM order: DOCUMENT_POSITION_PRECEDING is read as
      // "the launcher precedes the pane nav".
      const launcherPrecedesPane = launcher && paneNav
        ? !!(paneNav.compareDocumentPosition(launcher) & Node.DOCUMENT_POSITION_PRECEDING)
        : null;
      return {
        launcherCount: launchers.length,
        launcherVisible: !!launcher,
        launcherPrecedesPane,
        launcherBottom: launcher ? rect(launcher).bottom : null,
        paneTop: paneNav ? rect(paneNav).top : null,
        tabHeights: tabs.map(tab => rect(tab).height),
        shellBottom: shell ? rect(shell).bottom : null,
        workspaceTop: workspace ? rect(workspace).top : null,
        overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      };
    }""")
    assert metrics["launcherCount"] == 1, metrics
    assert metrics["launcherVisible"], metrics
    assert metrics["launcherPrecedesPane"] is True, metrics
    assert metrics["launcherBottom"] <= metrics["paneTop"] + 1, metrics
    assert metrics["tabHeights"] and min(metrics["tabHeights"]) >= 44, metrics
    assert not metrics["overflow"], metrics
    assert metrics["workspaceTop"] >= metrics["shellBottom"] - 1, metrics

    # Real canvas geometry on the graph pane (the compact route boots into
    # the concepts pane, where the canvas is intentionally not rendered).
    page.evaluate("""() => { document.querySelector('[data-graph-pane="graph"]').click(); }""")
    expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", "graph")
    canvas_ok = page.evaluate("""() => { const c = document.querySelector('#okf-graph canvas');
      const r = c && c.getBoundingClientRect();
      return !!r && r.width > 100 && r.height > 100; }""")
    assert canvas_ok
    camera = page.evaluate("""() => { const g = window.__okfLoomGraph;
      return {pan: g.cy.pan(), zoom: g.cy.zoom()}; }""")

    # A pane round-trip (graph → concepts → graph) must preserve the user
    # camera: graph.js owns refit on pane change, and no chrome edit here
    # introduces a second geometry owner.
    page.evaluate("""() => { document.querySelector('[data-graph-pane="concepts"]').click(); }""")
    expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", "concepts")
    page.evaluate("""() => { document.querySelector('[data-graph-pane="graph"]').click(); }""")
    expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", "graph")
    page.wait_for_function(
        "() => { const g = window.__okfLoomGraph; if (!g || !g.cy) return false;"
        " const p = g.cy.pan(), z = g.cy.zoom();"
        " return Math.abs(p.x - %f) < 0.5 && Math.abs(p.y - %f) < 0.5 && Math.abs(z - %f) < 1e-6; }"
        % (camera["pan"]["x"], camera["pan"]["y"], camera["zoom"]),
    )
    assert page.locator(".okf-studio-launcher").count() == 1


@pytest.mark.parametrize("width", [900, 901, 1280, 1440])
def test_graph_heading_is_a_compact_row_above_a_usable_workspace(page, server_url: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1")
    metrics = page.evaluate("""() => {
      const rect=selector=>document.querySelector(selector).getBoundingClientRect().toJSON();
      return {heading:rect('.okf-graph-route-title'),workspace:rect('.okf-layout'),canvas:rect('#okf-graph')};
    }""")
    assert metrics["heading"]["height"] <= 56, metrics
    assert metrics["heading"]["bottom"] <= metrics["workspace"]["top"] + 1, metrics
    assert metrics["workspace"]["width"] >= width - 1, metrics
    assert metrics["canvas"]["width"] >= (width - 1 if width <= 900 else width * 0.5), metrics


@pytest.mark.parametrize("width", [641, 768, 900])
def test_tablet_shell_nav_controls_meet_the_44px_usability_floor(
    page, server_url: str, width: int,
) -> None:
    """U2: in the 641–900px band every global nav control reaches a 44px
    touch floor (measured bounding boxes, not a CSS source assertion).

    This is a usability recommendation, not a WCAG 2.5.8 finding — the
    former ~30px links passed the 24px AA minimum. The floor must not turn
    into a giant shell or an overflow: the shell stays two compact rows and
    every control stays inside the viewport."""
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1")
    metrics = page.evaluate("""() => {
      const shell = document.querySelector('[data-okf-application-shell]');
      const nav = shell.querySelector('.okf-shell__nav');
      const controls = [...nav.querySelectorAll('a, button, input')]
        .filter(el => el.getClientRects().length > 0)
        .map(el => { const r = el.getBoundingClientRect();
          return {name: el.getAttribute('aria-label') || el.textContent.trim() || el.tagName,
            height: r.height, left: r.left, right: r.right}; });
      return {controls,
        shellHeight: shell.getBoundingClientRect().height,
        overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
        clipped: controls.filter(c => c.left < -0.5 || c.right > window.innerWidth + 0.5).map(c => c.name)};
    }""")
    assert metrics["controls"], metrics
    short = [c for c in metrics["controls"] if c["height"] < 44]
    assert not short, {"short": short, "metrics": metrics}
    assert not metrics["overflow"] and metrics["clipped"] == [], metrics
    # Usability floor, not a giant shell: brand row + one nav row stay bounded.
    assert metrics["shellHeight"] <= 170, metrics


def test_tablet_floor_stops_at_the_900px_edge(page, server_url: str) -> None:
    """U2 edge sanity: 901px is ordinary desktop — the shell keeps its compact
    single-row fit and gains no forced inflation from the tablet floor."""
    page.set_viewport_size({"width": 901, "height": 900})
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1")
    metrics = page.evaluate("""() => {
      const shell = document.querySelector('[data-okf-application-shell]');
      return {shellHeight: shell.getBoundingClientRect().height,
        overflow: document.documentElement.scrollWidth > window.innerWidth + 1};
    }""")
    assert not metrics["overflow"] and metrics["shellHeight"] <= 110, metrics


# Ledger's 320px drawer and 20px page gutters leave the measured 600px prose
# minimum at 960px. Test both sides of that boundary; the article's border
# box includes the gutters, so it still clears the 620px invariant below.
@pytest.mark.parametrize("width,rail", [(900, False), (901, False), (959, False), (960, True), (1024, True), (1100, True), (1280, True), (1440, True)])
def test_studio_rail_requires_usable_document_width(page, server_url: str, width: int, rail: bool) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.commentsReady")
    page.locator('.okf-studio-launcher').click()
    metrics = page.evaluate("""() => { const article=document.querySelector('.okf-page'); const panel=document.querySelector('#okf-panel');
      const ar=article.getBoundingClientRect(), pr=panel.getBoundingClientRect(); return {rail:document.body.classList.contains('okf-panel-rail'),
      role:panel.getAttribute('role'),modal:panel.getAttribute('aria-modal'),articleWidth:ar.width,articleLeft:ar.left,articleRight:ar.right,
      panelLeft:pr.left,overflow:document.documentElement.scrollWidth>innerWidth+1}; }""")
    assert metrics["rail"] is rail and not metrics["overflow"], metrics
    if rail:
        assert metrics["role"] == "complementary" and metrics["modal"] is None
        assert metrics["articleWidth"] >= 620 and metrics["articleRight"] <= metrics["panelLeft"] + 1, metrics
    else:
        assert metrics["role"] == "dialog" and metrics["modal"] == "true"
        assert metrics["articleWidth"] >= min(width - 32, 620), metrics


def test_shell_search_submit_focus_provenance_enter_go_back_reload_and_storage_denial(page, server_url: str) -> None:
    page.goto(server_url + "/")
    search = page.locator('.okf-shell__search-form input')
    search.fill('orders')
    search.press('Enter')
    page.wait_for_url(lambda url: '/__search' in url)
    assert page.evaluate("document.activeElement === document.querySelector('[data-okf-route-heading]')")
    page.reload()
    assert not page.evaluate("document.activeElement === document.querySelector('[data-okf-route-heading]')")
    page.go_back()
    assert not page.evaluate("document.activeElement === document.querySelector('[data-okf-route-heading]')")

    page.add_init_script("Object.defineProperty(window,'sessionStorage',{get(){throw new DOMException('denied')}})")
    page.goto(server_url + "/")
    page.locator('.okf-shell__search-form input').fill('users')
    page.locator('.okf-shell__search-submit').click()
    page.wait_for_url(lambda url: '/__search' in url)
    assert page.evaluate("document.activeElement === document.querySelector('[data-okf-route-heading]')")
    assert page.evaluate("document.activeElement !== document.body")


def test_shell_search_enter_and_submit_button_reach_actual_results(
    page, server_url: str,
) -> None:
    page.goto(server_url + "/")
    search = page.locator('.okf-shell__search-form input')
    search.fill("orders")
    search.press("Enter")
    page.wait_for_url(lambda url: "/__search" in url and "q=orders" in url)
    assert page.locator(".okf-search-result").count() >= 1
    assert page.locator('.okf-shell__search-form input').get_attribute("value") == "orders"

    page.goto(server_url + "/")
    page.locator('.okf-shell__search-form input').fill("refund")
    page.locator('.okf-shell__search-submit').click()
    page.wait_for_url(lambda url: "/__search" in url and "q=refund" in url)
    assert page.locator(".okf-search-result").count() >= 1

    # A hostile query stays escaped: the value attribute round-trips the raw
    # text and no script element is injected into the document.
    hostile = '"><script>alert(1)</script>'
    page.goto(server_url + "/")
    page.locator('.okf-shell__search-form input').fill(hostile)
    page.locator('.okf-shell__search-form input').press("Enter")
    page.wait_for_url(lambda url: "/__search" in url)
    echoed = page.locator('.okf-shell__search-form input').get_attribute("value")
    assert echoed == hostile, echoed
    assert "<script>alert" not in page.content()


def test_search_five_default_results_fit_reproducible_geometry(page, server_url: str) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(server_url + "/__search?q=orders&limit=5")
    results = page.locator(".okf-search-result")
    expect(results).to_have_count(5)
    geometry = page.evaluate("""() => { const rows=[...document.querySelectorAll('.okf-search-result')];
      return {bottom:rows.at(-1).getBoundingClientRect().bottom+scrollY,
        overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth+1,
        complete:rows.every(row=>row.querySelector('.okf-search-snippet')&&row.querySelector('.okf-search-evidence-summary')&&
          row.querySelector('.okf-search-open')&&row.querySelector('[data-search-action="copy-passage"]')&&
          row.querySelector('.okf-search-disclosure'))}; }""")
    # Four readable snippet lines deliberately replace the former two-line
    # density target. Bound each result, not the old aggregate pixel count.
    for row in results.all():
        assert row.bounding_box()['height'] <= 420
        assert not row.locator('.okf-search-disclosure').evaluate('el => el.open')
    assert geometry["overflow"] is False and geometry["complete"] is True, geometry


def test_authoritative_resync_preserves_only_post_snapshot_live_events_and_deduplicates(page, server_url: str) -> None:
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.eventsReady")
    result = page.evaluate("""async () => {
      const S=window.okfLoomStudio, original=window.fetch.bind(window);
      const comments=await (await original('/__comments')).json();
      const envelope=await (await original('/__data/events?limit=500')).json();
      window.okfLoomLive.emit('activity',{event_id:'stale-snapshot-event',id:'same-domain',type:'activity',action:'update'});
      let release; const gate=new Promise(resolve=>release=resolve);
      window.fetch=(url,opts)=>{
        if(String(url).startsWith('/__comments')) return gate.then(()=>new Response(JSON.stringify(comments),{status:200,headers:{'Content-Type':'application/json'}}));
        if(String(url).startsWith('/__data/events')) return gate.then(()=>new Response(JSON.stringify(envelope),{status:200,headers:{'Content-Type':'application/json'}}));
        return original(url,opts);
      };
      const pending=S._loadComments(0,{authoritative:true});
      window.okfLoomLive.emit('activity',{event_id:'live-survivor',id:'same-domain',type:'activity',action:'update'});
      window.okfLoomLive.emit('activity',{event_id:'live-without-domain-id',type:'activity',action:'update'});
      release(); await pending;
      const once=S.state.events.filter(e=>['stale-snapshot-event','live-survivor','live-without-domain-id'].includes(e.event_id)).map(e=>e.event_id);
      const canonical=JSON.parse(JSON.stringify(envelope));
      canonical.events=(canonical.events||[]).concat([
        {event_id:'live-survivor',id:'same-domain',type:'activity',action:'update'},
        {event_id:'live-without-domain-id',type:'activity',action:'update'},
        {event_id:'live-survivor',id:'same-domain',type:'activity',action:'duplicate'}
      ]);
      window.fetch=(url,opts)=>{
        if(String(url).startsWith('/__comments')) return Promise.resolve(new Response(JSON.stringify(comments),{status:200,headers:{'Content-Type':'application/json'}}));
        if(String(url).startsWith('/__data/events')) return Promise.resolve(new Response(JSON.stringify(canonical),{status:200,headers:{'Content-Type':'application/json'}}));
        return original(url,opts);
      };
      await S._loadComments(0,{authoritative:true});
      window.fetch=original;
      const twice=S.state.events.filter(e=>['stale-snapshot-event','live-survivor','live-without-domain-id'].includes(e.event_id)).map(e=>e.event_id);
      return {once,twice,unread:[...S.state.attention.unread]};
    }""")
    assert result["once"].count("stale-snapshot-event") == 0, result
    assert result["once"].count("live-survivor") == 1 and result["once"].count("live-without-domain-id") == 1, result
    assert len(result["twice"]) == len(set(result["twice"])), result
    assert "stale-snapshot-event" not in result["unread"], result


def test_graph_tour_is_opt_in_and_restores_invitation_focus(page, server_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script("try{localStorage.clear()}catch(e){}")
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1")
    assert page.get_by_role('dialog', name='Graph tour').count() == 0
    invitation = page.get_by_role('button', name='Take a 45-second graph tour', include_hidden=True)
    reveal_control(page, invitation)
    expect(invitation).to_be_visible()
    invitation.click()
    expect(page.get_by_role('dialog', name='Graph tour')).to_be_visible()
    page.keyboard.press('Escape')
    expect(invitation).to_be_focused()
    assert page.locator('.okf-graph-tour-scrim').count() == 0


@pytest.mark.parametrize("kind", ["input", "select", "textarea", "contenteditable", "textbox"])
@pytest.mark.parametrize("width,closes", [(600, True), (1440, False)])
def test_studio_direct_escape_matrix_for_modal_and_nonmodal_editables(page, server_url: str, width: int, closes: bool, kind: str) -> None:
    page.set_viewport_size({"width": width, "height": 850})
    page.goto(server_url + "/tables/orders")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.commentsReady")
    page.locator('.okf-studio-launcher').click()
    page.evaluate("""kind => { const host=document.querySelector('#okf-panel-body'); let el;
      if(kind==='contenteditable'){el=document.createElement('div');el.contentEditable='true';}
      else if(kind==='textbox'){el=document.createElement('div');el.setAttribute('role','textbox');el.tabIndex=0;}
      else {el=document.createElement(kind); if(kind==='select')el.appendChild(new Option('One','1'));}
      el.id='escape-matrix-target';el.setAttribute('aria-label','Escape matrix target');host.appendChild(el);el.focus(); }""", kind)
    page.keyboard.press('Escape')
    assert page.locator('#okf-panel').is_hidden() is closes
    if closes:
        expect(page.locator('.okf-studio-launcher')).to_be_focused()
    else:
        assert page.evaluate("document.activeElement && document.activeElement.id === 'escape-matrix-target'")


# ---- Scroll-away chrome on phones ------------------------------------------
# At <=640px the reading view keeps at most one compact sticky row (the view
# bar's Sections row) while the reader scrolls down; the full chrome returns
# on scroll-up, at the top, for keyboard focus, open overlays and captures.

_CHROME_GEOMETRY = """() => {
  const box = sel => { const n = document.querySelector(sel);
    if (!n || !n.getClientRects().length) return null;
    const r = n.getBoundingClientRect(); return {top: r.top, bottom: r.bottom, height: r.height}; };
  const top = box('.okf-topbar'), bar = box('.okf-concept-viewbar');
  const jump = box('.okf-concept-viewbar .okf-outline-jump');
  const sticky = Math.max(0, ...[top, bar].filter(Boolean).map(b => Math.min(innerHeight, b.bottom)));
  return {state: document.documentElement.getAttribute('data-okf-chrome'), top, bar, jump, sticky,
    y: scrollY, overflow: document.documentElement.scrollWidth > innerWidth + 1,
    topTransition: getComputedStyle(document.querySelector('.okf-topbar')).transitionDuration};
}"""

_HITS = """selectors => selectors.map(sel => {
  const n = document.querySelector(sel);
  if (!n) return [sel, 'missing'];
  const r = n.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return [sel, !!hit && (hit === n || n.contains(hit))];
}).filter(([, ok]) => ok !== true)"""

_SHELL_CONTROLS = ['.okf-shell__search-form input', '[data-okf-shell-item="index"] a',
                   '[data-okf-shell-item="graph"] a', '.okf-appearance-trigger']


def _frames(page) -> None:
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def _scroll(page, dy: int) -> None:
    page.evaluate("dy => window.scrollBy(0, dy)", dy)
    _frames(page)


def _settle(page) -> None:
    page.wait_for_function("""() => !document.getAnimations().some(a =>
      a.transitionProperty === 'transform' && a.playState === 'running')""")
    _frames(page)


def _phone_page(playwright, width: int, height: int, coarse: bool, **options):
    browser = launch_chromium(playwright)
    context = browser.new_context(viewport={"width": width, "height": height},
                                  is_mobile=coarse, has_touch=coarse, **options)
    return browser, context


def _wait_live_document(page) -> None:
    page.wait_for_function("""() => window.okfStudioReadiness && window.okfStudioReadiness.ready &&
      document.querySelector('.okf-concept-viewbar [data-okf-outline-jump]')""")
    page.evaluate("() => document.fonts.ready")
    _frames(page)


@pytest.mark.parametrize("width,height,coarse", [(390, 844, True), (390, 844, False),
                                                 (320, 700, True), (320, 700, False)])
def test_phone_chrome_scrolls_away_to_one_row_and_returns_on_scroll_up(
    server_url: str, width: int, height: int, coarse: bool,
) -> None:
    with sync_playwright() as playwright:
        browser, context = _phone_page(playwright, width, height, coarse)
        try:
            context.add_init_script(_FIRST_PAINT_CLS)
            page = context.new_page()
            page.goto(server_url + "/tables/orders")
            _wait_live_document(page)
            at_top = page.evaluate(_CHROME_GEOMETRY)
            assert at_top["state"] == "revealed" and at_top["top"]["top"] == 0, at_top
            # Compact rows: the top bar takes two rows at 390px (Studio shares
            # the search row), three at 320px; the view tabs keep one row with
            # Sections on its own row below; the search placeholder is whole.
            layout = page.evaluate("""() => {
              const rows = sel => { const tops = [...document.querySelectorAll(sel)]
                .filter(n => n.getClientRects().length).map(n => n.getBoundingClientRect().top).sort((a, b) => a - b);
                return tops.filter((t, i) => !i || t - tops[i - 1] > 8).length; };
              const input = document.querySelector('.okf-shell__search-form input'), style = getComputedStyle(input);
              const ctx = document.createElement('canvas').getContext('2d'); ctx.font = style.font;
              const tabs = [...document.querySelectorAll('.okf-concept-viewbar .okf-viewswitch__btn')]
                .filter(n => n.getClientRects().length);
              return {shellRows: rows('.okf-shell [data-okf-shell-item]'),
                tabRows: rows('.okf-concept-viewbar .okf-viewswitch__btn'), tabs: tabs.map(n => n.textContent.trim()),
                jumpBelowTabs: document.querySelector('.okf-outline-jump').getBoundingClientRect().top >=
                  Math.max(...tabs.map(n => n.getBoundingClientRect().bottom)),
                placeholderFits: ctx.measureText(input.placeholder).width + parseFloat(style.paddingLeft) +
                  parseFloat(style.paddingRight) <= input.clientWidth};
            }""")
            assert layout["shellRows"] <= (2 if width >= 390 else 3), layout
            assert layout["tabRows"] == 1 and "Focus view" in layout["tabs"], layout
            assert layout["jumpBelowTabs"] and layout["placeholderFits"], layout

            _scroll(page, 300)
            _scroll(page, 300)
            _settle(page)
            down = page.evaluate(_CHROME_GEOMETRY)
            assert down["state"] == "compact", down
            assert down["sticky"] <= 56, down
            assert down["top"]["bottom"] <= 0, down
            assert 0 <= down["jump"]["top"] <= 8, down
            assert page.evaluate(_HITS, ['.okf-outline-jump__toggle']) == []

            _scroll(page, -50)
            _settle(page)
            up = page.evaluate(_CHROME_GEOMETRY)
            assert up["state"] == "revealed" and abs(up["top"]["top"]) < 0.5, up
            assert abs(up["bar"]["top"] - up["top"]["bottom"]) < 1, up
            assert page.evaluate(_HITS, _SHELL_CONTROLS + ['.okf-studio-launcher']) == []
            assert up["overflow"] is False, up

            # Back to the top keeps the full chrome; nothing shifted throughout.
            _scroll(page, -5000)
            _settle(page)
            assert page.evaluate(_CHROME_GEOMETRY)["state"] == "revealed"
            result = page.evaluate("window.__okfPaintCLS()")
            assert result["cls"] < 0.02, result
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("coarse", [True, False])
def test_phone_chrome_returns_for_keyboard_focus_overlays_menus_and_captures(server_url: str, coarse: bool) -> None:
    with sync_playwright() as playwright:
        browser, context = _phone_page(playwright, 390, 844, coarse)
        try:
            page = context.new_page()
            page.goto(server_url + "/tables/orders")
            _wait_live_document(page)
            _scroll(page, 600)
            _settle(page)
            assert page.evaluate(_CHROME_GEOMETRY)["state"] == "compact"
            y = page.evaluate("scrollY")

            # The pinned Sections row is already on screen: focusing it moves
            # nothing. Shift+Tab into the hidden view tabs, then the top bar,
            # brings the whole chrome back at once without scrolling the page.
            page.locator('.okf-outline-jump__toggle').focus()
            _frames(page)
            pinned = page.evaluate(_CHROME_GEOMETRY)
            assert pinned["state"] == "compact" and pinned["top"]["bottom"] <= 0, pinned
            page.keyboard.press("Shift+Tab")
            revealed = page.evaluate(_CHROME_GEOMETRY)
            assert revealed["state"] == "revealed" and abs(revealed["top"]["top"]) < 0.5, revealed
            assert page.evaluate("document.activeElement.closest('.okf-viewswitch') !== null")
            for _ in range(8):
                if page.evaluate("document.activeElement.closest('.okf-shell') !== null"):
                    break
                page.keyboard.press("Shift+Tab")
            assert page.evaluate("document.activeElement.closest('.okf-shell') !== null")
            assert page.evaluate("""() => { const n = document.activeElement, r = n.getBoundingClientRect();
              return r.top >= 0 && r.bottom <= innerHeight; }""")
            assert page.evaluate("scrollY") == y

            # Reading resumes: focus back in the document, scroll down, compact.
            page.evaluate("() => { const h = document.querySelector('.okf-page__title'); h.focus({preventScroll: true}); }")
            _scroll(page, 200)
            _settle(page)
            assert page.evaluate(_CHROME_GEOMETRY)["state"] == "compact"

            # The command palette (a registered overlay) brings the chrome back.
            page.keyboard.press("Control+k")
            expect(page.locator('.okf-palette-overlay')).to_be_visible()
            assert page.evaluate(_CHROME_GEOMETRY)["state"] == "revealed"
            page.keyboard.press("Escape")
            expect(page.locator('.okf-palette-overlay')).to_be_hidden()

            # The Appearance menu keeps the chrome while it is open.
            page.locator('.okf-appearance-trigger').click()
            expect(page.locator('#okf-appearance-menu')).to_be_visible()
            _scroll(page, 300)
            _settle(page)
            assert abs(page.evaluate(_CHROME_GEOMETRY)["top"]["top"]) < 0.5
            page.keyboard.press("Escape")
            expect(page.locator('#okf-appearance-menu')).to_be_hidden()
            page.evaluate("() => { const h = document.querySelector('.okf-page__title'); h.focus({preventScroll: true}); }")
            _scroll(page, 300)
            _settle(page)
            assert page.evaluate(_CHROME_GEOMETRY)["top"]["bottom"] <= 0

            # A capture always gets the full chrome, immediately (no slide).
            page.evaluate("() => { window.okfCaptureReadiness.start(); }")
            captured = page.evaluate(_CHROME_GEOMETRY)
            assert abs(captured["top"]["top"]) < 0.5 and captured["topTransition"] == "0s", captured
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("coarse", [True, False])
def test_phone_fragment_and_sections_jumps_land_below_the_pinned_row(server_url: str, coarse: bool) -> None:
    with sync_playwright() as playwright:
        browser, context = _phone_page(playwright, 390, 844, coarse)
        try:
            page = context.new_page()
            page.goto(server_url + "/tables/orders")
            _wait_live_document(page)
            heading_id = page.evaluate("""() => [...document.querySelectorAll('.okf-page__body :is(h2,h3)[id]')]
              .filter(h => h.getBoundingClientRect().top + scrollY > 900).map(h => h.id)[0] || null""")
            assert heading_id, "the demo page needs a heading below the fold"
            below_row = """id => { const h = document.getElementById(id).getBoundingClientRect();
              const box = sel => { const n = document.querySelector(sel); return n.getBoundingClientRect(); };
              const chrome = Math.max(0, box('.okf-topbar').bottom, box('.okf-concept-viewbar').bottom);
              return {heading: h.top, chrome, state: document.documentElement.getAttribute('data-okf-chrome')}; }"""

            page.goto("about:blank")
            page.goto(f"{server_url}/tables/orders#{heading_id}")
            _wait_live_document(page)
            page.wait_for_function("id => document.activeElement && document.activeElement.id === id", arg=heading_id)
            _settle(page)
            landed = page.evaluate(below_row, heading_id)
            assert landed["chrome"] - 1 <= landed["heading"] <= landed["chrome"] + 40, landed

            # Sections popover activation from the top of the page.
            _scroll(page, -10000)
            _settle(page)
            page.locator('.okf-outline-jump__toggle').click()
            page.locator(f'#okf-outline-jump-panel a[href="#{heading_id}"]').click()
            _settle(page)
            jumped = page.evaluate(below_row, heading_id)
            assert jumped["state"] == "compact", jumped
            assert jumped["chrome"] <= 56, jumped
            assert jumped["chrome"] - 1 <= jumped["heading"] <= jumped["chrome"] + 40, jumped
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_phone_chrome_slides_only_without_reduced_motion(server_url: str, motion: str) -> None:
    with sync_playwright() as playwright:
        browser, context = _phone_page(playwright, 390, 844, True, reduced_motion=motion)
        try:
            page = context.new_page()
            page.goto(server_url + "/tables/orders")
            _wait_live_document(page)
            _scroll(page, 600)
            moving = page.evaluate(_CHROME_GEOMETRY)
            assert moving["state"] == "compact", moving
            if motion == "reduce":
                assert moving["topTransition"] == "0s" and moving["top"]["bottom"] <= 0, moving
                assert page.evaluate("document.getAnimations().length") == 0
            else:
                assert moving["topTransition"] == "0.18s", moving
        finally:
            context.close()
            browser.close()


def test_wide_and_graph_chrome_keep_their_sticky_layout(page, server_url: str) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(server_url + "/tables/orders")
    _wait_live_document(page)
    _scroll(page, 600)
    wide = page.evaluate(_CHROME_GEOMETRY)
    assert wide["state"] is None and wide["top"]["top"] == 0, wide
    assert abs(wide["bar"]["top"] - wide["top"]["bottom"]) < 1, wide
    assert page.evaluate("getComputedStyle(document.querySelector('.okf-topbar')).transform") == "none"

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(server_url + "/__graph")
    page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1")
    assert page.evaluate("document.documentElement.hasAttribute('data-okf-chrome')") is False
    assert page.evaluate("getComputedStyle(document.querySelector('.okf-topbar')).transform") == "none"


@pytest.fixture(scope="module")
def portable_sites(tmp_path_factory):
    """The demo bundle built as static and SPA sites, each served at its own root
    (the SPA links its assets from "/")."""
    root = tmp_path_factory.mktemp("portable-chrome")
    with contextlib.ExitStack() as stack:
        sites = {}
        for target in ("static", "spa"):
            out = root / target
            result = subprocess.run(
                okf_module_argv("build", str(DEMO_BUNDLE), "--target", target, "--out", str(out)),
                cwd=str(TOOLKIT_ROOT), env=okf_subprocess_env(), capture_output=True, text=True,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            port = free_port()
            sites[target] = f"http://127.0.0.1:{port}"
            stack.enter_context(managed_server(
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(out)],
                sites[target], root / f"http-{target}-{port}.log"))
        yield sites


@pytest.mark.parametrize("target", ["static", "spa"])
@pytest.mark.parametrize("width,height,coarse", [(390, 844, True), (320, 700, False)])
def test_portable_phone_top_bar_scrolls_away_and_returns(
    portable_sites: dict, target: str, width: int, height: int, coarse: bool,
) -> None:
    with sync_playwright() as playwright:
        browser, context = _phone_page(playwright, width, height, coarse)
        try:
            context.add_init_script(_FIRST_PAINT_CLS)
            page = context.new_page()
            page.goto(f"{portable_sites[target]}/tables/orders.html")
            page.wait_for_function("() => document.documentElement.getAttribute('data-okf-chrome') === 'revealed'")
            page.evaluate("() => document.fonts.ready")
            assert page.locator('.okf-concept-viewbar').count() == 0
            _scroll(page, 300)
            _scroll(page, 300)
            _settle(page)
            down = page.evaluate(_CHROME_GEOMETRY)
            assert down["state"] == "compact" and down["sticky"] == 0, down
            _scroll(page, -50)
            _settle(page)
            up = page.evaluate(_CHROME_GEOMETRY)
            assert up["state"] == "revealed" and abs(up["top"]["top"]) < 0.5, up
            assert page.evaluate(_HITS, _SHELL_CONTROLS) == []
            assert page.evaluate("window.__okfPaintCLS()")["cls"] < 0.02
        finally:
            context.close()
            browser.close()
