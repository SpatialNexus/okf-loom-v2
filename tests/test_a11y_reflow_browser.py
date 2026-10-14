"""Chromium reflow-equivalent and accessibility-semantics regression checks (Q5 A-D).

Reflow is exercised with CSS-viewport equivalents: a viewport plus a matching
device scale factor. 720x450 at DPR 2 is 200% of 1440x900, and 320x256 at
DPR 4 is 400% of 1280x1024, which covers both WCAG 1.4.10 dimensions. These
reproduce the layout viewport that 1.4.10 is defined in, but they are not
browser-owned zoom and nothing here claims zoom. Semantics come from
Playwright's ARIA snapshot and Chromium's accessibility tree (CDP), so these
are Chromium-only regression guards, not a conformance claim.

The Q5 fixes are guarded here: the skip link lands below the sticky chrome
(wiki.css #okf-main scroll-margin-top, measured on concept pages, nominal on
the index), the shell and document view bar scroll with the page on short
narrow viewports (shell.css / studio.css) and section jumps drop the pinned
offset there (wiki.css), the search route names its workbench landmark and
field apart from the shell search, and the graph route names its Details
complementary landmark.

Q7 measures WCAG 1.4.3 contrast of muted and secondary text (.okf-muted, the
Comments, Changes and Agent activity purpose lines, the composer precision
line and the search result meta line) in every Appearance theme, chosen
through the Appearance control, from computed colours against the composited
ancestor background. The same measurement covers code blocks: every
highlight.js token colour, plain and untagged code, inline code and the code
block badge/copy chrome against the computed code background. Light themes
must never paint a surface pure white.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from browser_support import copy_bundle, expect, launch_chromium, live_server, reveal_control, sync_playwright

pytestmark = pytest.mark.browser
TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DEMO_BUNDLE = TOOLKIT_ROOT / "samples" / "demo_bundle"

CONCEPT = "/tables/orders"
INDEX = "/"
SEARCH = "/__search?q=orders"
GRAPH = "/__graph"

# (CSS width, CSS height, device scale factor)
DESKTOP = (1440, 900, 1)
REFLOW_200 = (720, 450, 2)
REFLOW_400 = (320, 256, 4)

STUDIO_READY = "() => window.okfStudioReadiness && window.okfStudioReadiness.ready"
GRAPH_SETTLED = "() => window.okfGraphCaptureState && window.okfGraphCaptureState().layoutSettled"
TEXT_SPACING = ("*{line-height:1.5!important;letter-spacing:.12em!important;"
                "word-spacing:.16em!important} p{margin-bottom:2em!important}")


def _viewport_id(viewport) -> str:
    width, height, dpr = viewport
    return f"{width}x{height}@{dpr}"


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("a11y-reflow") / "bundle")
    with live_server(bundle) as base:
        yield base


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def open_page(chromium, server_url):
    """Open a route in a fresh context sized to one CSS-viewport equivalent."""
    contexts = []

    def _open(path: str, viewport):
        width, height, dpr = viewport
        context = chromium.new_context(viewport={"width": width, "height": height},
                                       device_scale_factor=dpr)
        contexts.append(context)
        page = context.new_page()
        page.goto(server_url + path)
        page.wait_for_function(STUDIO_READY)
        if path.startswith(GRAPH):
            page.wait_for_function(GRAPH_SETTLED)
        metrics = page.evaluate("() => [innerWidth, innerHeight, devicePixelRatio]")
        assert metrics == [width, height, dpr], f"viewport equivalent not applied: {metrics}"
        return page

    yield _open
    for context in contexts:
        context.close()


@pytest.fixture(scope='module')
def coarse_server_url(tmp_path_factory):
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp('coarse-targets') / 'bundle')
    concept = bundle / 'tables/orders.md'
    concept.write_text(concept.read_text() + ''.join(f'\n## Extra section {i}\n\nText.\n' for i in range(12)))
    for path in ['demo/annotations_v2_showcase.md', 'explanation/v2_release_notes.md']:
        file = bundle / path
        file.parent.mkdir(exist_ok=True)
        file.write_text('---\ntype: Reference\ntitle: Coarse proof\n---\n# Coarse proof\n')
    with live_server(bundle) as base:
        yield base


@pytest.mark.parametrize('route,selectors', [
    (CONCEPT, ['.okf-sidebar-panel__toggle', '.okf-heading-anchor', '.okf-breadcrumb a',
               '.okf-conn__title', '.okf-viewswitch__btn', '.okf-onthispage__filter',
               '.okf-toast__action', '.okf-toast__close']),
    (INDEX, ['.okf-card__link', '.okf-card__comments', '.okf-v2-spotlight__tertiary']),
    (SEARCH, ['.okf-search-mode', '.okf-search-disclosure > summary',
              '.okf-search-query input', '.okf-search-submit']),
])
def test_core_coarse_pointer_targets_are_44px_at_390(chromium, coarse_server_url, route, selectors):
    server_url = coarse_server_url
    context = chromium.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    try:
        page = context.new_page()
        page.goto(server_url + CONCEPT)
        page.wait_for_function(STUDIO_READY)
        assert page.evaluate("matchMedia('(pointer: coarse)').matches")
        # Produce a real comment so inventory comment links are exposed.
        page.locator('.okf-studio-launcher').click()
        page.locator('#okf-panel').get_by_role('button', name='Comments', exact=True).click()
        comment = f'Coarse-pointer target proof for {route}.'
        page.get_by_role('textbox', name='Comment for agent').fill(comment)
        page.get_by_role('button', name='Send', exact=True).click()
        # Wait on the persistent canonical card, not a short-lived toast.
        expect(page.locator('.okf-comment__body', has_text=comment)).to_be_visible(timeout=15000)
        page.goto(server_url + route)
        page.wait_for_function(STUDIO_READY)
        if route == CONCEPT:
            page.locator('.okf-onthispage > summary').click()
            disclosure = page.locator('.okf-connections-disclosure > summary')
            if disclosure.count():
                disclosure.click()
            page.evaluate("""() => {
              const action = document.createElement('button'); action.className = 'okf-toast__action';
              action.textContent = 'View comment';
              action.addEventListener('click', () => { window.__coarseAction = true; });
              window.okfLoomStudio._toast(action, {sticky: true});
            }""")
        for selector in selectors:
            targets = page.locator(selector).evaluate_all("""nodes => nodes.filter(n => n.checkVisibility()).map(n => {
              const r = n.getBoundingClientRect(); return {text: n.textContent, width: r.width, height: r.height};
            })""")
            assert targets, f'{route}: no visible target for {selector}'
            assert all(t['width'] >= 43.5 and t['height'] >= 43.5 for t in targets), (route, selector, targets)
        if route == CONCEPT:
            page.locator('.okf-toast__action').click()
            assert page.evaluate('window.__coarseAction')
            page.locator('.okf-toast__close').click()
            expect(page.locator('.okf-toast__action')).to_have_count(0)
        overflow = page.evaluate(_OVERFLOW)
        assert overflow['scrollWidth'] <= overflow['innerWidth'] + 1, overflow
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['bone', 'slate', 'clay', 'plum'])
def test_ledger_hover_press_and_breadcrumb_cues_remain_visible(open_page, theme):
    page = open_page(CONCEPT, DESKTOP)
    _choose_appearance(page, theme)
    separators = page.locator('.okf-breadcrumb__sep')
    assert separators.count() > 0
    for i in range(separators.count()):
        expect(separators.nth(i)).to_be_visible()
    rows = page.evaluate(_TEXT_CONTRAST, '.okf-breadcrumb__sep')
    assert rows and all(row['ratio'] >= 4.5 for row in rows), rows
    page.locator('.okf-studio-launcher').click()
    button = page.locator('#okf-panel').get_by_role('button', name='Comments', exact=True)
    colour = 'b => getComputedStyle(b).backgroundColor'
    idle = button.evaluate(colour)
    button.hover()
    hovered = button.evaluate(colour)
    page.mouse.down()
    pressed = button.evaluate(colour)
    page.mouse.up()
    assert idle != hovered and hovered != pressed, (theme, idle, hovered, pressed)


def test_search_modes_keep_native_keyboard_semantics_with_square_indicators(open_page):
    page = open_page(SEARCH, DESKTOP)
    radios = page.locator('.okf-search-mode input')
    first = radios.first
    first.focus()
    page.keyboard.press('ArrowRight')
    expect(radios.nth(1)).to_be_checked()
    expect(radios.nth(1)).to_be_focused()
    assert first.evaluate("r => getComputedStyle(r).borderRadius") == '0px'
    assert page.locator('.okf-search-query input').bounding_box()['height'] == 32
    page.emulate_media(forced_colors='active')
    visible = radios.nth(1).evaluate("""r => { const s = getComputedStyle(r), dot = getComputedStyle(r, '::before');
      return s.borderTopStyle === 'solid' && dot.backgroundColor !== s.backgroundColor; }""")
    assert visible


def _settle(page) -> None:
    """Wait until scrolling and CSS transitions are at rest (frame state, not time)."""
    settled = page.evaluate("""() => new Promise(resolve => {
      let lastX = scrollX, lastY = scrollY, still = 0, frames = 0;
      const tick = () => {
        const moving = scrollX !== lastX || scrollY !== lastY
          || document.getAnimations().some(a => a instanceof CSSTransition && a.playState === 'running');
        still = moving ? 0 : still + 1;
        lastX = scrollX; lastY = scrollY;
        if (still >= 2) resolve(true);
        else if (++frames > 600) resolve(false);
        else requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    })""")
    assert settled, "scrolling or CSS transitions never came to rest"


# Horizontal overflow: the page must not scroll sideways, and no visible
# element may cross the viewport edge unless it lives inside a
# keyboard-reachable scroll container that itself fits the viewport (the
# accepted table and code scrollers), or is the graph canvas.
_OVERFLOW = """() => {
  const contained = el => {
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      if (!/auto|scroll/.test(getComputedStyle(a).overflowX)) continue;
      const r = a.getBoundingClientRect();
      if (r.left >= -1 && r.right <= innerWidth + 1 && a.tabIndex >= 0) return true;
    }
    return false;
  };
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || getComputedStyle(el).visibility === 'hidden') continue;
    if (r.left >= -1 && r.right <= innerWidth + 1) continue;
    if (el.closest('#okf-graph .cytoscape-container') || contained(el)) continue;
    offenders.push(`${el.tagName}.${String(el.className).slice(0, 50)} [${Math.round(r.left)}, ${Math.round(r.right)}]`);
  }
  return {scrollWidth: document.documentElement.scrollWidth, innerWidth, offenders: offenders.slice(0, 12)};
}"""

_CLIPPED_TEXT = """(root) => {
  const clips = s => /hidden|clip/.test(s.overflowX + ' ' + s.overflowY)
    || s.textOverflow === 'ellipsis' || (s.webkitLineClamp && s.webkitLineClamp !== 'none');
  const visible = n => { const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 1 && r.height > 1; };
  return [root, ...root.querySelectorAll('*')].filter(n => {
    if (!visible(n) || n.closest('.okf-search-snippet, .okf-card__desc') || !clips(getComputedStyle(n))) return false;
    return n.scrollWidth > n.clientWidth + 1 || n.scrollHeight > n.clientHeight + 1;
  }).map(n => `${n.tagName}.${String(n.className).slice(0, 50)} ${n.scrollWidth}x${n.scrollHeight} > ${n.clientWidth}x${n.clientHeight}`);
}"""

_CONTROL_BOX = """(el) => {
  const r = el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {left: r.left, top: r.top, right: r.right, bottom: r.bottom, height: r.height,
          covered: !(hit && (hit === el || el.contains(hit))),
          hit: hit ? `${hit.tagName}.${String(hit.className).slice(0, 40)}` : null};
}"""

# WCAG 2.4.11 (minimum): the focused element must not be ENTIRELY hidden, so
# it passes when any of its centre or inset corners is the topmost element.
_FOCUS_NOT_HIDDEN = """() => {
  const el = document.activeElement, r = el.getBoundingClientRect();
  const points = [[.5, .5], [.1, .1], [.9, .1], [.1, .9], [.9, .9]]
    .map(([x, y]) => [r.left + r.width * x, r.top + r.height * y])
    .filter(([x, y]) => x >= 0 && y >= 0 && x < innerWidth && y < innerHeight);
  const hits = points.map(([x, y]) => document.elementFromPoint(x, y));
  return {inMain: !!el.closest('#okf-main'),
          focused: `${el.tagName}.${String(el.className).slice(0, 40)} "${(el.innerText || '').trim().slice(0, 30)}"`,
          top: Math.round(r.top), seen: hits.some(h => h && (h === el || el.contains(h))),
          coveredBy: hits.filter(h => h && h !== el && !el.contains(h)).map(h => `${h.tagName}.${String(h.className).slice(0, 30)}`)[0] || null};
}"""


def _shell_controls(page) -> dict:
    banner = page.get_by_role("banner")
    search = banner.get_by_role("search")
    return {
        "brand": banner.get_by_role("link", name="OKF Demo Bundle", exact=True),
        "search": search.get_by_label("Search this bundle", exact=True),
        "submit": search.get_by_role("button", name="Search", exact=True),
        "index": banner.get_by_role("link", name="Index", exact=True),
        "graph": banner.get_by_role("link", name="Graph", exact=True),
        "studio": banner.get_by_role("button", name=re.compile(r"^Studio")),
        "appearance": banner.get_by_role("button", name=re.compile(r"^Appearance")),
    }


def _ax_roles(page) -> list[tuple[str, str]]:
    """(role, name) for every exposed node in Chromium's accessibility tree."""
    cdp = page.context.new_cdp_session(page)
    try:
        nodes = cdp.send("Accessibility.getFullAXTree")["nodes"]
    finally:
        cdp.detach()
    exposed = []
    for node in nodes:
        if node.get("ignored"):
            continue
        role = (node.get("role") or {}).get("value", "")
        name = (node.get("name") or {}).get("value", "")
        if role == "heading" and any(prop.get("name") == "level" and prop["value"].get("value") == 1
                                     for prop in node.get("properties", [])):
            role = "heading-1"
        exposed.append((role, name))
    return exposed


def _count(roles, role: str) -> int:
    return sum(1 for candidate, _ in roles if candidate == role)


# ---------------------------------------------------------------- A. reflow

@pytest.mark.parametrize("viewport", [REFLOW_200, REFLOW_400], ids=_viewport_id)
def test_concept_shell_reflows_at_css_viewport_equivalent(open_page, viewport) -> None:
    page = open_page(CONCEPT, viewport)
    overflow = page.evaluate(_OVERFLOW)
    assert overflow["scrollWidth"] <= overflow["innerWidth"] + 1, overflow
    assert overflow["offenders"] == [], overflow

    width, height, _ = viewport
    boxes = {}
    for key, control in _shell_controls(page).items():
        expect(control).to_have_count(1)
        expect(control).to_be_visible()
        box = control.evaluate(_CONTROL_BOX)
        assert -1 <= box["left"] and box["right"] <= width + 1, (key, box)
        assert -1 <= box["top"] and box["bottom"] <= height + 1, (key, box)
        assert not box["covered"], (key, box)
        assert control.evaluate(_CLIPPED_TEXT) == [], key
        # 44px touch floor for the nav controls (shell.css <=640 block and
        # 641-900 band); the brand link keeps the 2.5.8 24px minimum.
        assert box["height"] >= (24 if key == "brand" else 44) - 0.5, (key, box)
        boxes[key] = box

    if viewport == REFLOW_400:
        # shell.css <=340px: brand / search, Index, Graph / Studio, Appearance.
        assert boxes["brand"]["bottom"] <= boxes["search"]["top"] + 1, boxes
        for key in ("submit", "index", "graph"):
            assert abs(boxes[key]["top"] - boxes["search"]["top"]) <= 2, (key, boxes)
        assert boxes["studio"]["top"] >= boxes["graph"]["bottom"] - 1, boxes
        assert abs(boxes["appearance"]["top"] - boxes["studio"]["top"]) <= 2, boxes


@pytest.mark.parametrize("route, start, viewport", [
    pytest.param(CONCEPT, "tab-from-top", REFLOW_200, id="tab-from-top-720x450@2"),
    # The ~165px wrapped shell plus the ~106px view bar exceed a 256px
    # viewport, so both scroll with the page there (WCAG 1.4.10 / 2.4.11).
    pytest.param(CONCEPT, "tab-from-top", REFLOW_400, id="tab-from-top-320x256@4"),
    # Skip to content lands #okf-main below the measured sticky stack.
    pytest.param(CONCEPT, "after-skip-link", DESKTOP, id="after-skip-link-1440x900@1"),
    pytest.param(CONCEPT, "after-skip-link", REFLOW_200, id="after-skip-link-720x450@2"),
    # The index route has no measured stack; it clears the nominal shell.
    pytest.param(INDEX, "after-skip-link", REFLOW_200, id="index-after-skip-link-720x450@2"),
])
def test_focus_in_main_is_not_hidden_by_sticky_chrome(open_page, route, start, viewport) -> None:
    page = open_page(route, viewport)
    if start == "after-skip-link":
        page.keyboard.press("Tab")
        expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
        page.keyboard.press("Enter")
        _settle(page)
    stops = []
    for _ in range(30):
        page.keyboard.press("Tab")
        _settle(page)
        stop = page.evaluate(_FOCUS_NOT_HIDDEN)
        if stop["inMain"]:
            stops.append(stop)
        if len(stops) == 5:
            break
    assert len(stops) == 5, stops
    hidden = [f"{stop['focused']} at y={stop['top']} under {stop['coveredBy']}"
              for stop in stops if not stop["seen"]]
    assert hidden == [], hidden


def test_short_narrow_viewport_keeps_the_reading_area_clear_mid_document(open_page) -> None:
    """At 320x256 neither the shell nor the document view bar stays pinned:
    mid-document, main content fills the viewport from top to bottom."""
    page = open_page(CONCEPT, REFLOW_400)
    page.mouse.move(160, 128)
    page.mouse.wheel(0, 1500)
    _settle(page)
    assert page.evaluate("() => scrollY") > 600
    samples = page.evaluate("""() => [0.05, 0.25, 0.5, 0.75, 0.95].map(f => {
      const hit = document.elementFromPoint(innerWidth / 2, innerHeight * f);
      return {y: Math.round(innerHeight * f), inMain: !!(hit && hit.closest('#okf-main')),
              hit: hit ? `${hit.tagName}.${String(hit.className).slice(0, 30)}` : null};
    })""")
    assert [sample for sample in samples if not sample["inMain"]] == [], samples


_HEADING_IN_VIEW = """(el) => {
  const r = el.getBoundingClientRect();
  const points = [[.5, .5], [.1, .1], [.9, .1], [.1, .9], [.9, .9]]
    .map(([x, y]) => [r.left + r.width * x, r.top + r.height * y]);
  const inside = points.every(([x, y]) => x >= 0 && y >= 0 && x < innerWidth && y < innerHeight);
  const hits = inside ? points.map(([x, y]) => document.elementFromPoint(x, y)) : [];
  return {top: Math.round(r.top), bottom: Math.round(r.bottom), innerHeight, inside,
          coveredBy: hits.filter(h => !h || (h !== el && !el.contains(h))).map(h => h ? `${h.tagName}.${String(h.className).slice(0, 30)}` : null)};
}"""


@pytest.mark.parametrize("viewport", [DESKTOP, REFLOW_400], ids=_viewport_id)
def test_section_jump_lands_heading_in_view_and_clear_of_chrome(open_page, viewport) -> None:
    """A section jump clears the pinned chrome; where the chrome scrolls with
    the page (320x256) it must not keep the pinned-stack offset, which is
    taller than the viewport and would land the heading below the fold."""
    page = open_page(CONCEPT, viewport)
    outline = page.get_by_role("group", name="On this page")
    outline.locator("summary").click()
    sections = outline.get_by_role("navigation", name="Document sections")
    sections.get_by_role("link", name="Notes", exact=True).click()
    heading = page.locator(".okf-page__body h2#notes")
    expect(heading).to_have_count(1)
    _settle(page)
    result = heading.evaluate(_HEADING_IN_VIEW)
    assert result["inside"] and result["coveredBy"] == [], result


def test_studio_panel_is_a_labelled_modal_dialog_at_320_css_viewport_equivalent(open_page) -> None:
    page = open_page(CONCEPT, REFLOW_400)
    launcher = page.locator(".okf-studio-launcher")
    launcher.focus()
    page.keyboard.press("Enter")
    panel = page.locator("#okf-panel")
    expect(panel).to_be_visible()
    expect(page.get_by_role("dialog", name="Studio")).to_have_count(1)
    expect(panel).to_have_attribute("aria-modal", "true")
    expect(panel).to_have_attribute("aria-labelledby", "okf-panel-title")
    overflow = page.evaluate(_OVERFLOW)
    assert overflow["scrollWidth"] <= overflow["innerWidth"] + 1, overflow

    inside = "() => document.getElementById('okf-panel').contains(document.activeElement)"
    for key in ["Tab"] * 20 + ["Shift+Tab"] * 5:
        page.keyboard.press(key)
        assert page.evaluate(inside), f"{key} left the modal Studio dialog"

    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(launcher).to_be_focused()


# ------------------------------------------------------- B. text spacing

_SURFACES = {
    # surface: (route, scopes whose controls are checked, scope that must not be empty)
    "concept": (CONCEPT, (".okf-shell", ".okf-concept-viewbar", ".okf-document-tools",
                          'nav[aria-label="Breadcrumb"]'), 'nav[aria-label="Breadcrumb"]'),
    "search": (SEARCH, (".okf-shell", ".okf-search-workbench"), ".okf-search-workbench"),
    "graph-details": (GRAPH, (".okf-shell", ".okf-graph-mobile-nav", "#okf-detail"), "#okf-detail"),
    "concept-studio": (CONCEPT, (".okf-shell", ".okf-concept-viewbar", ".okf-document-tools",
                                 'nav[aria-label="Breadcrumb"]', "#okf-panel .okf-panel__header"),
                       "#okf-panel .okf-panel__header"),
}

_SPACED_CONTROLS = """(scopes) => {
  const clips = s => /hidden|clip/.test(s.overflowX + ' ' + s.overflowY)
    || s.textOverflow === 'ellipsis' || (s.webkitLineClamp && s.webkitLineClamp !== 'none');
  const visible = n => { const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 1 && r.height > 1; };
  const counts = {}, clipped = [], seen = new Set();
  for (const scope of scopes) {
    counts[scope] = 0;
    for (const root of document.querySelectorAll(scope)) {
      for (const control of root.querySelectorAll('a, button, label, summary, legend, [role=tab]')) {
        if (!visible(control)) continue;
        counts[scope] += 1;
        for (const n of [control, ...control.querySelectorAll('*')]) {
          if (seen.has(n) || !visible(n)) continue;
          seen.add(n);
          if (n.closest('.okf-search-snippet, .okf-card__desc') || !clips(getComputedStyle(n))) continue;
          if (n.scrollWidth > n.clientWidth + 1 || n.scrollHeight > n.clientHeight + 1)
            clipped.push(`${n.tagName}.${String(n.className).slice(0, 50)} "${(n.innerText || '').trim().slice(0, 40)}" `
              + `${n.scrollWidth}x${n.scrollHeight} > ${n.clientWidth}x${n.clientHeight}`);
        }
      }
    }
  }
  const probe = document.querySelector('.okf-shell .okf-btn');
  const style = getComputedStyle(probe);
  return {counts, clipped, overflow: document.documentElement.scrollWidth - innerWidth,
          letterSpacingEm: parseFloat(style.letterSpacing) / parseFloat(style.fontSize)};
}"""


@pytest.mark.parametrize("viewport", [DESKTOP, REFLOW_400], ids=_viewport_id)
@pytest.mark.parametrize("surface", list(_SURFACES))
def test_text_spacing_override_keeps_controls_unclipped(open_page, surface, viewport) -> None:
    route, scopes, required = _SURFACES[surface]
    page = open_page(route, viewport)
    if surface == "graph-details":
        details = page.get_by_role("navigation", name="Graph panes").get_by_role("button", name="Details", exact=True)
        if details.is_visible():
            details.click()
            expect(details).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#okf-detail")).to_be_visible()
    if surface == "concept-studio":
        page.locator(".okf-studio-launcher").click()
        expect(page.locator("#okf-panel")).to_be_visible()
    page.add_style_tag(content=TEXT_SPACING)
    _settle(page)

    result = page.evaluate(_SPACED_CONTROLS, list(scopes))
    assert abs(result["letterSpacingEm"] - 0.12) < 0.01, result  # the override really applied
    assert result["counts"][required] > 0, result
    assert result["overflow"] <= 1, result
    assert result["clipped"] == [], result


# ------------------------------------------------- C. keyboard traversal

_RECORD_STOP = """() => {
  const el = document.activeElement;
  (window.__q5Stops = window.__q5Stops || []).push(el);
  const s = getComputedStyle(el), r = el.getBoundingClientRect();
  return {focused: `${el.tagName}.${String(el.className).slice(0, 40)} "${(el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 30)}"`,
          outline: [s.outlineStyle, parseFloat(s.outlineWidth) || 0, s.outlineColor], shadow: s.boxShadow,
          onScreen: r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth};
}"""


def _has_visible_focus(stop: dict, unfocused_shadow: str) -> bool:
    style, width, color = stop["outline"]
    transparent = color in ("transparent", "rgba(0, 0, 0, 0)")
    if style not in ("none", "hidden") and width >= 1 and not transparent:
        return True
    return stop["shadow"] != "none" and stop["shadow"] != unfocused_shadow


@pytest.mark.parametrize("viewport", [DESKTOP, REFLOW_400], ids=_viewport_id)
def test_main_shell_keyboard_traversal_order(open_page, viewport) -> None:
    page = open_page(CONCEPT, viewport)
    shell = _shell_controls(page)
    view = page.get_by_role("region", name="Document view")
    expected = [page.get_by_role("link", name="Skip to content"), *shell.values(),
                view.get_by_role("group", name="View mode").get_by_role("button").first]

    stops = []
    for locator in expected:
        page.keyboard.press("Tab")
        expect(locator).to_be_focused()
        _settle(page)
        stops.append(page.evaluate(_RECORD_STOP))
    in_view = "(region) => region.contains(document.activeElement)"
    for _ in range(12):
        page.keyboard.press("Tab")
        _settle(page)
        stops.append(page.evaluate(_RECORD_STOP))
        if not view.evaluate(in_view):
            break
    expect(page.get_by_role("navigation", name="Breadcrumb").get_by_role("link").first).to_be_focused()
    assert len(stops) >= 10, stops

    # Visible focus: an opaque outline, or a box-shadow that only focus adds.
    page.keyboard.press("Tab")
    _settle(page)
    unfocused = page.evaluate("() => window.__q5Stops.map(el => getComputedStyle(el).boxShadow)")
    invisible = [stop["focused"] for stop, shadow in zip(stops, unfocused)
                 if not stop["onScreen"] or not _has_visible_focus(stop, shadow)]
    assert invisible == [], invisible

    # Shift+Tab walks the same stops back exactly.
    for index in range(len(stops) - 1, -1, -1):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("(i) => document.activeElement === window.__q5Stops[i]", index), (
            f"Shift+Tab did not return to {stops[index]['focused']}")

    # The skip link hands the next Tab stop to the main landmark.
    page.goto(page.url)
    page.wait_for_function(STUDIO_READY)
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    assert page.evaluate("() => !!document.activeElement.closest('#okf-main')")


def test_studio_rail_is_a_non_modal_complementary_landmark_tab_can_leave(open_page) -> None:
    page = open_page(CONCEPT, DESKTOP)
    page.locator(".okf-studio-launcher").click()
    rail = page.get_by_role("complementary", name="Studio")
    expect(rail).to_be_visible()
    assert rail.get_attribute("aria-modal") is None
    left_rail = ("() => document.activeElement !== document.body"
                 " && !document.getElementById('okf-panel').contains(document.activeElement)")
    for _ in range(40):
        page.keyboard.press("Tab")
        if page.evaluate(left_rail):
            break
    else:
        pytest.fail("Tab never left the non-modal Studio rail")


# ------------------------------------------------ D. landmarks and names

def _snapshot_has(snapshot: str, line: str) -> bool:
    return re.search(r"^\s*- " + re.escape(line) + r"(?::|$)", snapshot, re.MULTILINE) is not None


def test_concept_route_landmarks_and_names(open_page) -> None:
    page = open_page(CONCEPT, DESKTOP)
    roles = _ax_roles(page)
    assert (_count(roles, "banner"), _count(roles, "main"), _count(roles, "heading-1")) == (1, 1, 1), roles

    snapshot = page.locator("body").aria_snapshot()
    for line in ('navigation "Primary navigation"', 'region "Document view"', "article",
                 'navigation "Breadcrumb"', 'group "On this page"', 'navigation "Document utilities"',
                 'navigation "Document end"', 'complementary "Related concepts"'):
        assert _snapshot_has(snapshot, line), f"missing {line}"
    assert _snapshot_has(page.get_by_role("region", name="Document view").aria_snapshot(), 'group "View mode"')
    expect(page.get_by_role("navigation", name="Breadcrumb").locator('[aria-current="page"]')).to_have_count(1)

    page.keyboard.press("Control+k")
    palette = page.get_by_role("dialog", name="Command palette")
    expect(palette).to_be_visible()
    expect(palette).to_have_attribute("aria-modal", "true")
    page.keyboard.press("Escape")
    expect(palette).to_be_hidden()


def test_search_route_structure(open_page) -> None:
    # Structure only: U4 is reworking the result content itself.
    page = open_page(SEARCH, DESKTOP)
    roles = _ax_roles(page)
    assert (_count(roles, "main"), _count(roles, "heading-1")) == (1, 1), roles
    heading = page.locator("#okf-results-heading").inner_text().strip()
    expect(page.locator('section[aria-labelledby="okf-results-heading"]')).to_have_count(1)
    expect(page.get_by_role("region", name=heading, exact=True)).to_have_count(1)
    expect(page.get_by_role("group", name="How should results be matched?")).to_have_count(1)


def test_graph_route_landmarks_and_names(open_page) -> None:
    page = open_page(GRAPH, REFLOW_400)
    snapshot = page.locator("body").aria_snapshot()
    for line in ('navigation "Graph shortcuts"', 'navigation "Graph panes"', 'search "Find in graph"',
                 'region "Concept graph"'):
        assert _snapshot_has(snapshot, line), f"missing {line}"
    page.get_by_role("navigation", name="Graph panes").get_by_role("button", name="Details", exact=True).click()
    expect(page.locator("#okf-detail")).to_be_visible()
    assert re.match(r"- complementary\b", page.locator("#okf-detail").aria_snapshot())


@pytest.mark.parametrize("route", [
    pytest.param(CONCEPT, id="concept"),
    # The workbench form is "Search this bundle" and its field "Search terms",
    # apart from the shell's search landmark and its "Search this bundle" field.
    pytest.param(SEARCH, id="search"),
    pytest.param(GRAPH, id="graph"),
])
def test_search_landmarks_and_fields_are_uniquely_named(open_page, route) -> None:
    roles = _ax_roles(open_page(route, DESKTOP))
    landmarks = [name for role, name in roles if role == "search"]
    assert landmarks, roles
    assert len(landmarks) == len(set(landmarks)), f"search landmarks share a name: {landmarks}"
    fields = [name for role, name in roles if role in ("searchbox", "combobox", "textbox")]
    assert sorted({name for name in fields if fields.count(name) > 1}) == [], fields


@pytest.mark.parametrize("route", [
    pytest.param(CONCEPT, id="concept"),
    # graph_page.html names its #okf-detail aside "Details".
    pytest.param(GRAPH, id="graph"),
])
def test_complementary_landmarks_are_named_with_the_studio_rail_open(open_page, route) -> None:
    page = open_page(route, DESKTOP)
    page.locator(".okf-studio-launcher").click()
    expect(page.get_by_role("complementary", name="Studio")).to_be_visible()
    names = [name for role, name in _ax_roles(page) if role == "complementary"]
    assert len(names) >= 2, names
    assert all(names), f"unnamed complementary landmark: {names}"
    assert len(set(names)) == len(names), names


# ------------------------------------------------ E. measured text contrast (Q7)

# Every Ledger palette (theme.js THEMES, kept in sync with render.py and
# config.py). The test also reads the runtime list, so a palette added there
# without a row here fails instead of going unmeasured.
APPEARANCE_THEMES = ("bone", "slate", "clay", "plum")

# WCAG 1.4.3 for every visible node that owns text under the matched elements.
# The effective background composites the background colours of the node and
# all its ancestors, so a transparent or translucent layer shows what is under
# it; the text colour, with the inherited opacity, is composited over that.
# A 1x1 canvas parses and composites, so any CSS colour syntax is handled. A
# background image above the nearest opaque colour cannot be measured this way
# and is reported instead of being guessed.
_TEXT_CONTRAST = r"""(selector) => {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  const read = () => ctx.getImageData(0, 0, 1, 1).data;
  const paint = (colour, alpha = 1) => { ctx.globalAlpha = alpha; ctx.fillStyle = colour; ctx.fillRect(0, 0, 1, 1); ctx.globalAlpha = 1; };
  const alphaOf = colour => { ctx.clearRect(0, 0, 1, 1); paint(colour); return read()[3] / 255; };
  const luminance = d => [d[0], d[1], d[2]].map(v => (v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
    .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  const hex = d => '#' + [d[0], d[1], d[2]].map(v => v.toString(16).padStart(2, '0')).join('');
  const rows = [];
  for (const target of document.querySelectorAll(selector)) {
    for (const node of [target, ...target.querySelectorAll('*')]) {
      if (![...node.childNodes].some(c => c.nodeType === Node.TEXT_NODE && c.textContent.trim())) continue;
      const box = node.getBoundingClientRect();
      if (box.width < 2 || box.height < 2 || !node.checkVisibility({checkOpacity: true, visibilityProperty: true})) continue;
      const chain = [];
      for (let a = node; a; a = a.parentElement) chain.unshift(a);
      let opaque = false, opacity = 1, images = [];
      const layers = chain.map(a => {
        const s = getComputedStyle(a);
        opacity *= parseFloat(s.opacity);
        if (alphaOf(s.backgroundColor) >= 1) { opaque = true; images = []; }
        if (s.backgroundImage !== 'none') images.push(a.tagName.toLowerCase() + '.' + String(a.className).split(' ')[0]);
        return s.backgroundColor;
      });
      ctx.clearRect(0, 0, 1, 1);
      paint('#ffffff');
      layers.forEach(colour => paint(colour));
      const bg = read();
      const style = getComputedStyle(node);
      paint(style.color, opacity);
      const fg = read();
      const [hi, lo] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
      const size = parseFloat(style.fontSize), weight = parseInt(style.fontWeight, 10);
      const large = size >= 24 || (size >= 18.66 && weight >= 700);
      rows.push({node: node.tagName.toLowerCase() + (node.className ? '.' + String(node.className).split(' ')[0] : ''),
                 text: node.textContent.trim().slice(0, 32), fg: hex(fg), bg: hex(bg),
                 ratio: Math.round((hi + 0.05) / (lo + 0.05) * 100) / 100, size, large,
                 required: large ? 3 : 4.5, opaque, images});
    }
  }
  return rows;
}"""


def _choose_appearance(page, theme: str) -> None:
    """Pick one Appearance palette the way a reader does (its label)."""
    trigger = page.locator("#okf-theme")
    menu = page.locator("#okf-appearance-menu")
    trigger.click()
    expect(menu).to_be_visible()
    option = menu.locator(f'input[data-okf-set="theme"][data-okf-val="{theme}"]')
    reveal_control(page, option)
    menu.locator(f'label.okf-appearance__opt[for="okf-appearance-theme-{theme}"]').click()
    expect(option).to_be_checked()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()


@pytest.mark.parametrize("theme", APPEARANCE_THEMES)
def test_muted_and_secondary_text_meets_wcag_aa_contrast_in_every_theme(open_page, server_url, theme) -> None:
    page = open_page(CONCEPT, DESKTOP)
    assert tuple(page.evaluate("() => window.OKFLoomTheme.THEMES")) == APPEARANCE_THEMES
    _choose_appearance(page, theme)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)

    measured: dict[str, list[dict]] = {}

    def measure(label: str, selector: str) -> None:
        measured[label] = page.evaluate(_TEXT_CONTRAST, selector)

    measure("concept .okf-muted", ".okf-muted")
    page.locator(".okf-studio-launcher").click()
    panel = page.locator("#okf-panel")
    expect(panel).to_be_visible()
    for name, extra in (("Comments", True), ("Changes", False), ("Agent activity", False)):
        panel.get_by_role("button", name=name, exact=name == "Comments").first.click()
        expect(page.locator("#okf-panel-title")).to_have_text(name)
        expect(page.locator("#okf-panel-purpose")).to_be_visible()
        measure(f"{name} purpose line", "#okf-panel-purpose")
        if extra:
            expect(page.get_by_role("textbox", name="Comment for agent")).to_be_visible()
            measure("composer precision line", ".okf-composer__anchor-note")
            measure("Studio panel .okf-muted", "#okf-panel .okf-muted")
        page.get_by_role("button", name="Back to Studio").click()

    for route, labels in ((SEARCH, (("search result meta line", ".okf-search-result__meta"),
                                    ("search .okf-muted", ".okf-muted"))),
                          (INDEX, (("index .okf-muted", ".okf-muted"),))):
        page.goto(server_url + route)
        page.wait_for_function(STUDIO_READY)
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        for label, selector in labels:
            measure(label, selector)

    unmeasured = [label for label, rows in measured.items() if not rows]
    assert not unmeasured, f"{theme}: no visible text measured for {unmeasured}"
    unresolved = [(label, row) for label, rows in measured.items() for row in rows
                  if not row["opaque"] or row["images"]]
    assert not unresolved, f"{theme}: background not measurable from colours: {unresolved[:4]}"
    failing = [(label, row["node"], row["text"], row["fg"], row["bg"], row["ratio"], row["required"])
               for label, rows in measured.items() for row in rows if row["ratio"] < row["required"]]
    assert not failing, f"{theme}: text below WCAG AA contrast: {failing[:8]}"


# Ledger status roles are text, icons and 2px bars, never fills. Two kinds of
# probe sit on a --surface panel: one line of text per status role (ok, warn,
# error, info), and the shipped studio.css status carriers (the toast tone
# icons, the resolved/claimed comment markers), built with the real classes
# (a claimed marker needs a live agent claim, a toast a mutation outcome) so
# the measured colours are whatever the cascade paints, fill or no fill.
_STATUS_ON_PROBES = """() => {
  const host = document.createElement('div');
  host.id = 'okf-status-on-probe';
  host.style.cssText = 'position:fixed;top:120px;left:40px;z-index:2147483647;display:flex;gap:12px;align-items:center;padding:8px;background:var(--surface)';
  for (const role of ['ok', 'warn', 'error', 'info']) {
    const line = document.createElement('span');
    line.className = 'okf-status-probe';
    line.style.color = `var(--status-${role})`;
    line.style.fontSize = '13px';
    line.textContent = role;
    host.append(line);
  }
  for (const tone of ['error', 'success', 'warn']) {
    const toast = document.createElement('div');
    toast.className = 'okf-toast';
    toast.dataset.tone = tone;
    toast.style.animation = 'none';
    const icon = document.createElement('span');
    icon.className = 'okf-toast__icon';
    icon.textContent = tone === 'error' ? '!' : tone === 'success' ? 'OK' : '?';
    toast.append(icon);
    host.append(toast);
  }
  for (const state of ['resolved', 'claimed']) {
    const marker = document.createElement('button');
    marker.type = 'button';
    marker.className = 'okf-comment-marker';
    marker.dataset.state = state;
    marker.style.position = 'relative';
    marker.textContent = '3';
    host.append(marker);
  }
  document.body.append(host);
}"""


@pytest.mark.parametrize("theme", APPEARANCE_THEMES)
def test_status_text_and_carriers_meet_wcag_aa_contrast_in_every_theme(open_page, theme) -> None:
    """Status hues are read as text: each --status-* role on --surface, and
    the toast icons / comment markers as the cascade paints them, reach AA.
    (The retired solid fills once put --okf-warn-on at 2.85-3.04:1.)"""
    page = open_page(CONCEPT, DESKTOP)
    _choose_appearance(page, theme)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    page.evaluate(_STATUS_ON_PROBES)
    rows = page.evaluate(_TEXT_CONTRAST, "#okf-status-on-probe .okf-status-probe, #okf-status-on-probe .okf-toast__icon, #okf-status-on-probe .okf-comment-marker")
    assert [row["text"] for row in rows] == ["ok", "warn", "error", "info", "!", "OK", "?", "3", "3"], rows
    # The role probes really paint the palette status hues on --surface.
    resolved = page.evaluate("""() => ['--status-ok', '--status-warn', '--status-error', '--status-info', '--surface'].map(t => {
      const p = document.createElement('i'); p.style.color = `var(${t})`; document.body.append(p);
      const c = getComputedStyle(p).color; p.remove(); return c; })""")
    painted = page.evaluate("""() => [...document.querySelectorAll('#okf-status-on-probe .okf-status-probe')].map(e => getComputedStyle(e).color)""")
    assert painted == resolved[:4], (theme, painted, resolved)
    assert page.evaluate("() => getComputedStyle(document.getElementById('okf-status-on-probe')).backgroundColor") == resolved[4]
    unresolved = [row for row in rows if not row["opaque"] or row["images"]]
    assert not unresolved, f"{theme}: background not measurable from colours: {unresolved}"
    failing = [(row["node"], row["text"], row["fg"], row["bg"], row["ratio"], row["required"])
               for row in rows if row["ratio"] < row["required"]]
    assert not failing, f"{theme}: status on-colour text below WCAG AA contrast: {failing}"


# ------------------------------------------ F. code blocks and light surfaces

# A concept whose language-tagged fences produce every highlight.js token
# class the code palette styles, beside inline code and an untagged block.
CODE_CONCEPT = "/code/highlight_tokens"
CODE_TOKEN_FIXTURE = '''---
type: Reference
title: Highlight tokens
description: Fenced code that exercises every styled highlight.js token class.
tags: [code]
---
# Highlight tokens

Inline `total(rows)` sits beside the fenced blocks.

```python
@dataclass
def total(rows: list) -> int:
    """Sum the rows."""
    # running total
    return sum(len(r) for r in rows) + 42 if True else None
```

```javascript
/**
 * @param {string} name who to greet
 */
function greet(name) {
  const pattern = /^[a-z]+$/i;
  return `hello ${name}`;
}
```

```html
<section class="card">Hi</section>
```

```css
body { margin: 0; }
```

```markdown
# Heading
- *emphasis* and **strong**
```

```ruby
status = :active
```

```
untagged block
```
'''
# Code whose first lines run under the block's badge and Copy button at any
# width (a tagged block and an untagged one).
LONG_LINE_CONCEPT = "/code/long_lines"
LONG_LINE_FIXTURE = """---
type: Reference
title: Long code lines
description: Code whose first line runs as far as the block tools.
---
# Long code lines

```bash
scripts/okf-loom validate docs-bundle --strict && scripts/okf-loom serve docs-bundle --no-open --tunnel --port 8791 --host 127.0.0.1
```

```
an untagged block whose first line is long enough to run past the copy button at every width, desktop included, before it scrolls
```
"""
# Token -> selector inside a highlighted block (hljs 11 scopes title.function
# as "hljs-title function_").
CODE_TOKENS = {
    "keyword": ".hljs-keyword", "doctag": ".hljs-doctag", "title": ".hljs-title",
    "function": ".hljs-title.function_", "string": ".hljs-string", "regexp": ".hljs-regexp",
    "number": ".hljs-number", "attr": ".hljs-attr", "literal": ".hljs-literal",
    "meta": ".hljs-meta", "section": ".hljs-section", "built_in": ".hljs-built_in",
    "symbol": ".hljs-symbol", "bullet": ".hljs-bullet", "comment": ".hljs-comment",
    "tag": ".hljs-tag", "name": ".hljs-name", "selector-tag": ".hljs-selector-tag",
    "subst": ".hljs-subst", "emphasis": ".hljs-emphasis", "strong": ".hljs-strong",
}
# Ledger code blocks are one dark well in every palette (Bone included):
# --code-bg / --code-text, with three syntax roles (--syn-1 keys/keywords,
# --syn-2 values, --syn-3 comments/punctuation) painted by wiki.css; no
# vendor theme sheet. Inline code is the quiet raised --icode-* chip. Every
# token in every palette must reach 4.5:1.
_CODE_ROLES = """() => { const s = getComputedStyle(document.documentElement);
  return Object.fromEntries(['--code-bg', '--code-text', '--icode-bg', '--icode-text', '--syn-1']
    .map(t => { const p = document.createElement('i'); p.style.color = s.getPropertyValue(t).trim();
                document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return [t, c]; })); }"""
RENDERERS_READY = """(theme) => { const r = window.okfRendererReadiness;
  return !!r && r.ready && r.themeName === theme
    && document.querySelectorAll('pre > code[class*="language-"]:not(.hljs)').length === 0; }"""

_CODE_COVERAGE = """(tokens) => Object.fromEntries(Object.entries(tokens).map(([name, sel]) =>
  [name, [...document.querySelectorAll('.okf-prose pre code.hljs ' + sel)].some(n => n.textContent.trim())]))"""

# Box styles that make tagged, untagged and inline code read as one system.
_CODE_BOXES = """() => {
  const pick = el => { const s = getComputedStyle(el);
    return {bg: s.backgroundColor, fg: s.color, border: s.borderTopWidth + ' ' + s.borderTopStyle + ' ' + s.borderTopColor,
            padding: s.padding, font: s.fontFamily, size: s.fontSize}; };
  const pres = [...document.querySelectorAll('.okf-prose pre')];
  const tagged = pres.find(p => p.querySelector('code.hljs'));
  const untagged = pres.find(p => !p.querySelector('code[class*="language-"]'));
  const inline = document.querySelector('.okf-prose :not(pre) > code');
  return {tagged: pick(tagged), untagged: pick(untagged), taggedCode: pick(tagged.querySelector('code')),
          untaggedCode: pick(untagged.querySelector('code')), inline: pick(inline),
          keyword: getComputedStyle(document.querySelector('.okf-prose .hljs-keyword')).color,
          section: getComputedStyle(document.querySelector('.okf-prose .hljs-section')).color};
}"""


@pytest.fixture(scope="module")
def code_server_url(tmp_path_factory):
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("code-tokens") / "bundle")
    (bundle / "code").mkdir()
    (bundle / "code" / "highlight_tokens.md").write_text(CODE_TOKEN_FIXTURE, encoding="utf-8")
    (bundle / "code" / "long_lines.md").write_text(LONG_LINE_FIXTURE, encoding="utf-8")
    with live_server(bundle) as base:
        yield base


@pytest.fixture
def open_code_page(chromium, code_server_url):
    contexts = []

    def _open(path: str, theme: str):
        context = chromium.new_context(viewport={"width": 1280, "height": 900})
        contexts.append(context)
        page = context.new_page()
        page.goto(code_server_url + path, wait_until="load")
        page.wait_for_function(STUDIO_READY)
        _choose_appearance(page, theme)
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        return page

    yield _open
    for context in contexts:
        context.close()


@pytest.mark.parametrize("theme", APPEARANCE_THEMES)
def test_code_block_tokens_and_chrome_meet_wcag_aa_contrast_in_every_theme(open_code_page, theme) -> None:
    page = open_code_page(CODE_CONCEPT, theme)
    page.wait_for_function(RENDERERS_READY, arg=theme)
    covered = page.evaluate(_CODE_COVERAGE, CODE_TOKENS)
    assert not [name for name, seen in covered.items() if not seen], f"{theme}: fixture token classes not rendered: {covered}"

    measured: dict[str, list[dict]] = {}
    for label, selector in (("code blocks", ".okf-prose pre"),
                            ("inline code", ".okf-prose :not(pre) > code"),
                            ("code language badge", ".okf-code-lang")):
        measured[label] = page.evaluate(_TEXT_CONTRAST, selector)
    page.locator(".okf-codewrap").first.hover()
    page.wait_for_function("() => getComputedStyle(document.querySelector('.okf-code-copy')).opacity === '1'")
    measured["code copy button"] = page.evaluate(_TEXT_CONTRAST, ".okf-codewrap .okf-code-copy")

    unmeasured = [label for label, rows in measured.items() if not rows]
    assert not unmeasured, f"{theme}: no visible text measured for {unmeasured}"
    unresolved = [(label, row) for label, rows in measured.items() for row in rows
                  if not row["opaque"] or row["images"]]
    assert not unresolved, f"{theme}: background not measurable from colours: {unresolved[:4]}"
    token_nodes = {row["node"] for row in measured["code blocks"]}
    # Token spans, plain text in a highlighted block, and the untagged block.
    assert {"span.hljs-keyword", "span.hljs-comment", "span.hljs-string",
            "code.language-python", "code"} <= token_nodes, token_nodes
    failing = [(label, row["node"], row["text"], row["fg"], row["bg"], row["ratio"], row["required"])
               for label, rows in measured.items() for row in rows if row["ratio"] < row["required"]]
    assert not failing, f"{theme}: code text below WCAG AA contrast: {failing[:8]}"

    boxes = page.evaluate(_CODE_BOXES)
    assert boxes["tagged"] == boxes["untagged"], f"{theme}: tagged and untagged blocks differ: {boxes}"
    # The <pre> owns padding in every theme; the vendor `pre code.hljs
    # { padding: 1em }` used to double-pad dark tagged code.
    assert boxes["taggedCode"] == boxes["untaggedCode"], f"{theme}: tagged and untagged code differ: {boxes}"
    roles = page.evaluate(_CODE_ROLES)
    assert (boxes["tagged"]["bg"], boxes["tagged"]["fg"]) == (roles["--code-bg"], roles["--code-text"]), (theme, boxes, roles)
    assert (boxes["inline"]["bg"], boxes["inline"]["fg"]) == (roles["--icode-bg"], roles["--icode-text"]), (theme, boxes, roles)
    assert boxes["keyword"] == boxes["section"] == roles["--syn-1"], (theme, boxes, roles)


# The light palettes (theme.js scheme "light"): Bone only.
LIGHT_THEMES = ("bone",)

# Bone's brightest surface is --surface #fbf9f5 (relative luminance ~0.949).
# Anything visibly painted brighter than that reads as the glare the user
# ruled out, pure white (1.0) included.
GLARE_LUMINANCE = 0.95
# Every visible element whose computed background is opaque and brighter than
# the glare ceiling. A 1x1 canvas parses any CSS colour syntax (oklch, color-mix).
_GLARING = """(ceiling) => {
  const ctx = Object.assign(document.createElement('canvas'), {width: 1, height: 1})
    .getContext('2d', {willReadFrequently: true});
  const lin = v => (v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  const out = [];
  for (const el of [document.documentElement, document.body, ...document.body.querySelectorAll('*')]) {
    const bg = getComputedStyle(el).backgroundColor, r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || !el.checkVisibility({visibilityProperty: true})) continue;
    ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = bg; ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    if (d[3] < 255) continue;
    const lum = 0.2126 * lin(d[0]) + 0.7152 * lin(d[1]) + 0.0722 * lin(d[2]);
    if (lum > ceiling) out.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}.${String(el.className).split(' ')[0]} ${bg} L=${lum.toFixed(3)}`);
  }
  return out;
}"""
_BACKGROUNDS = """(selectors) => Object.fromEntries(selectors.map(sel => {
  const el = document.querySelector(sel); return [sel, el ? getComputedStyle(el).backgroundColor : null]; }))"""


@pytest.mark.parametrize("theme", LIGHT_THEMES)
def test_light_theme_surfaces_are_never_pure_white(open_code_page, code_server_url, theme) -> None:
    page = open_code_page(CODE_CONCEPT, theme)
    page.wait_for_function(RENDERERS_READY, arg=theme)
    found: dict[str, list[str]] = {}
    named: dict[str, str | None] = {}

    def sweep(label: str, selectors: tuple[str, ...]) -> None:
        found[label] = page.evaluate(_GLARING, GLARE_LUMINANCE)
        for sel, bg in page.evaluate(_BACKGROUNDS, list(selectors)).items():
            named[f"{label} {sel}"] = bg

    sweep("concept", ("body", ".okf-topbar", ".okf-prose pre", ".okf-prose :not(pre) > code", ".okf-code-copy"))
    page.locator(".okf-studio-launcher").click()
    expect(page.locator("#okf-panel")).to_be_visible()
    sweep("studio", ("#okf-panel",))
    page.goto(code_server_url + INDEX, wait_until="load")
    page.wait_for_function(STUDIO_READY)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    sweep("index", ("body", ".okf-card"))
    page.goto(code_server_url + GRAPH, wait_until="load")
    page.wait_for_function(STUDIO_READY)
    page.wait_for_function(GRAPH_SETTLED)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    page.get_by_role("button", name="Show minimap").click()
    expect(page.locator(".okf-graph-minimap")).to_be_visible()
    sweep("graph", ("body", "#okf-graph", ".okf-graph-minimap", ".okf-detail"))

    missing = [sel for sel, bg in named.items() if bg is None]
    assert not missing, f"{theme}: expected surfaces not rendered: {missing}"
    white_named = [sel for sel, bg in named.items() if bg == "rgb(255, 255, 255)"]
    assert not white_named, f"{theme}: named surfaces are pure white: {white_named}"
    glaring = {label: nodes[:8] for label, nodes in found.items() if nodes}
    assert not glaring, f"{theme}: visible surfaces brighter than the light palette allows: {glaring}"


# ------------------------------------------------ G. type names (Q7 follow-up)

# Type colours (curated hsl 38-54% lightness, hash fallback hsl(h, 62%, 48%))
# measured 2.3-3.2:1 as text. Ledger retires them on the document surfaces: a
# type's NAME is palette ink, its identity is the name (and icon shape), and
# no per-type --okf-type-accent, tint or coloured underline is painted. The
# graph canvas (graph.css/graph.js, lane C) is measured for contrast only.
_RESOLVE_COLOUR = """(value) => { const probe = document.createElement('i'); probe.style.color = value;
  document.body.append(probe); const colour = getComputedStyle(probe).color; probe.remove(); return colour; }"""
_TYPE_CUES = """() => {
  const style = (sel, prop) => { const el = document.querySelector(sel); return el ? getComputedStyle(el)[prop] : null; };
  const accent = sel => { const el = document.querySelector(sel); return el ? el.style.getPropertyValue('--okf-type-accent').trim() : null; };
  return {
    infoboxAccent: accent('.okf-page__infobox'),
    infoboxIcon: style('.okf-infobox__type .okf-type-icon', 'color'),
  };
}"""


@pytest.mark.parametrize("theme", APPEARANCE_THEMES)
def test_type_names_meet_wcag_aa_contrast_and_carry_no_type_colour(open_code_page, code_server_url, theme) -> None:
    # CODE_CONCEPT is a Reference (formerly the amber type colour).
    page = open_code_page(CODE_CONCEPT, theme)
    measured: dict[str, list[dict]] = {}
    tinted: dict[str, object] = {}

    def measure(label: str, selector: str) -> None:
        measured[label] = page.evaluate(_TEXT_CONTRAST, selector)

    def goto(route: str) -> None:
        page.goto(code_server_url + route, wait_until="load")
        page.wait_for_function(STUDIO_READY)
        expect(page.locator("html")).to_have_attribute("data-theme", theme)

    def resolve(value: str) -> str:
        return page.evaluate(_RESOLVE_COLOUR, value)

    measure("concept header type chip", ".okf-page__header .okf-type-chip")
    measure("infobox type", ".okf-infobox__summary .okf-infobox__type")
    info = page.evaluate(_TYPE_CUES)
    if info["infoboxAccent"]:
        tinted["infobox inline type accent"] = info["infoboxAccent"]

    goto(CONCEPT)
    target = page.locator("a.okf-relation__target").first
    if not target.is_visible():
        # Typed relations live in the collapsed Document details disclosure.
        page.locator(".okf-page__infobox > .okf-infobox__summary").click()
        expect(page.locator(".okf-page__infobox")).to_have_attribute("open", "")
    reveal_control(page, target)
    target.hover()
    expect(page.locator("#okf-popover .okf-popover__type")).to_be_visible()
    measure("hovered relation target", "a.okf-relation__target:hover")
    hover = target.evaluate("""a => { const t = getComputedStyle(a.querySelector('.okf-relation__title'));
      return {line: t.textDecorationLine, colour: t.textDecorationColor, ink: t.color, accent: a.style.getPropertyValue('--okf-type-accent').trim()}; }""")
    assert "underline" in hover["line"], f"{theme}: hovered relation target has no hover cue: {hover}"
    if hover["colour"] != hover["ink"] or hover["accent"]:
        tinted["relation hover underline"] = hover
    measure("link popover type band", "#okf-popover .okf-popover__type")
    band = page.evaluate("""() => getComputedStyle(document.querySelector('#okf-popover .okf-popover__type')).backgroundColor""")
    if band != "rgba(0, 0, 0, 0)":
        tinted["popover type band fill"] = band

    goto(INDEX)
    # Ledger index rows are grouped under their type's section title; the
    # per-row type label is not shown, so the section title is the type name.
    measure("index section title", ".okf-section__title")
    assert page.evaluate("() => [...document.querySelectorAll('.okf-card__type')].every(e => !e.checkVisibility())")

    goto("/__search?q=highlight")
    measure("search result type", ".okf-search-result__type")
    page.locator('.okf-shell__search-form input[type="search"]').fill("orders")
    expect(page.locator(".okf-search-live__type").first).to_be_visible()
    measure("live suggestion type", ".okf-search-live__type")

    page.evaluate("() => localStorage.setItem('okfGraphTourDone', '1')")
    goto(GRAPH)
    page.wait_for_function(GRAPH_SETTLED)
    point = page.evaluate("""() => { const g = window.__okfLoomGraph, n = g.cy.nodes().filter(x => x.data('type') === 'Reference').first(),
      p = n.renderedPosition(), r = document.getElementById('okf-graph').getBoundingClientRect();
      return {x: r.left + p.x, y: r.top + p.y, id: n.id()}; }""")
    page.mouse.move(point["x"], point["y"])
    page.wait_for_function("id => window.__okfLoomGraph.interaction.snapshot().hovered === id", arg=point["id"])
    expect(page.locator(".okf-graph-tip__type")).to_be_visible()
    measure("graph hover tip type", ".okf-graph-tip__type")

    unmeasured = [label for label, rows in measured.items() if not rows]
    assert not unmeasured, f"{theme}: no visible type name measured for {unmeasured}"
    unresolved = [(label, row) for label, rows in measured.items() for row in rows
                  if not row["opaque"] or row["images"]]
    assert not unresolved, f"{theme}: background not measurable from colours: {unresolved[:4]}"
    failing = [(label, row["node"], row["text"], row["fg"], row["bg"], row["ratio"], row["required"])
               for label, rows in measured.items() for row in rows if row["ratio"] < row["required"]]
    assert not failing, f"{theme}: type names below WCAG AA contrast: {failing[:8]}"
    assert not tinted, f"{theme}: a type colour is still painted: {tinted}"



# ------------------------------------------- H. narrow code and Appearance menu

NARROW_WIDTHS = (390, 320)
CODE_READY = """() => { const r = window.okfRendererReadiness; const pres = document.querySelectorAll('.okf-prose pre');
  return !!r && r.ready && pres.length > 0 && document.querySelectorAll('.okf-prose .okf-codewrap .okf-code-tools').length === pres.length; }"""
# Each code block tool (language badge, Copy) against the code's line boxes:
# `covered` lists tools lying over code text, `reached` counts blocks whose text
# extends as far as a tool horizontally (so the fixture really tests overlap).
_CODE_TOOLS_OVER_TEXT = """() => { const covered = []; let reached = 0;
  document.querySelectorAll('.okf-prose .okf-codewrap').forEach((wrap, i) => {
    const code = wrap.querySelector('pre code') || wrap.querySelector('pre');
    const range = document.createRange(); range.selectNodeContents(code);
    const lines = [...range.getClientRects()].filter(r => r.width && r.height);
    const tools = [...wrap.querySelectorAll('.okf-code-lang, .okf-code-copy')].map(el => [el.className, el.getBoundingClientRect()]);
    if (tools.some(([, t]) => lines.some(r => r.right > t.left))) reached += 1;
    for (const [name, t] of tools)
      if (lines.some(r => r.bottom > t.top + 0.5 && r.top < t.bottom - 0.5 && r.right > t.left + 0.5 && r.left < t.right - 0.5))
        covered.push(`block ${i} ${name}`);
  });
  return {covered, reached}; }"""
# Every place a text node wraps between two non-space characters (a word broken
# across lines), skipping breaks after a hyphen or slash.
_MIDWORD_BREAKS = r"""(root) => { const out = [];
  const walker = document.createTreeWalker(document.querySelector(root), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent;
    if (!text.trim() || !node.parentElement.checkVisibility()) continue;
    let previous = null;
    for (let i = 0; i < text.length; i++) {
      const range = document.createRange(); range.setStart(node, i); range.setEnd(node, i + 1);
      const box = range.getClientRects()[0];
      if (!box || !box.width) continue;
      const top = Math.round(box.top);
      if (previous !== null && top > previous + 2 && /\S/.test(text[i - 1]) && /\S/.test(text[i]) && !'-/'.includes(text[i - 1]))
        out.push(text.slice(Math.max(0, i - 8), i) + '|' + text.slice(i, i + 8));
      previous = top;
    }
  }
  return out; }"""


@pytest.fixture
def open_width(chromium, code_server_url):
    contexts = []

    def _open(path: str, width: int):
        context = chromium.new_context(viewport={"width": width, "height": 844})
        contexts.append(context)
        page = context.new_page()
        page.add_init_script("localStorage.setItem('okfGraphTourDone', '1')")
        page.goto(code_server_url + path, wait_until="load")
        page.wait_for_function(STUDIO_READY)
        if path.startswith(GRAPH):
            page.wait_for_function(GRAPH_SETTLED)
        return page

    yield _open
    for context in contexts:
        context.close()


@pytest.mark.parametrize("width", (390, 1280))
def test_code_block_badge_and_copy_never_cover_code_text(open_width, width) -> None:
    """The absolutely placed tools row covered the end of the first code line
    (15 of 20 spec blocks at 390px, long lines at 1280px)."""
    page = open_width(LONG_LINE_CONCEPT, width)
    page.wait_for_function(CODE_READY)
    result = page.evaluate(_CODE_TOOLS_OVER_TEXT)
    assert result["reached"] >= 2, f"fixture lines do not reach the tools at {width}px: {result}"
    assert not result["covered"], f"code tools cover code text at {width}px: {result['covered']}"


@pytest.mark.parametrize("width", NARROW_WIDTHS)
@pytest.mark.parametrize("route", [pytest.param(LONG_LINE_CONCEPT, id="concept"), pytest.param(GRAPH, id="graph")])
def test_appearance_menu_breaks_lines_only_between_words(open_width, route, width) -> None:
    """The graph topbar's narrow reflow inherits `overflow-wrap: anywhere`,
    which let option labels shrink below their longest word ("Comp|act")."""
    page = open_width(route, width)
    page.locator("#okf-theme").click()
    menu = page.locator("#okf-appearance-menu")
    expect(menu).to_be_visible()
    advanced = menu.locator("details")
    for index in range(advanced.count()):
        if advanced.nth(index).get_attribute("open") is None:
            advanced.nth(index).locator("summary").click()
    assert menu.locator('input[data-okf-set="theme"]').first.is_visible()
    box = menu.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width, f"menu leaves the {width}px viewport: {box}"
    breaks = page.evaluate(_MIDWORD_BREAKS, "#okf-appearance-menu")
    assert not breaks, f"Appearance menu breaks words at {width}px on {route}: {breaks}"



# Non-focusable parts of the open Appearance dialog: a pointer press there used
# to blur the focused option to <body>, and focusout (no related target) closed
# the menu under the pointer.
_MENU_SPOTS = (
    ("group legend", lambda menu: _centre(menu.locator("legend").first)),
    ("group padding", lambda menu: _corner(menu.locator(".okf-appearance__group").first, 4)),
    ("dialog padding", lambda menu: _corner(menu, 3)),
)


def _centre(locator) -> tuple[float, float]:
    box = locator.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _corner(locator, inset: float) -> tuple[float, float]:
    box = locator.bounding_box()
    return box["x"] + box["width"] - inset, box["y"] + inset


@pytest.mark.parametrize("route", [pytest.param(LONG_LINE_CONCEPT, id="concept"), pytest.param(GRAPH, id="graph")])
def test_appearance_menu_stays_open_when_its_non_focusable_parts_are_clicked(open_width, route) -> None:
    page = open_width(route, 1280)
    trigger = page.locator("#okf-theme")
    menu = page.locator("#okf-appearance-menu")
    trigger.click()
    expect(menu).to_be_visible()
    inside = "() => document.getElementById('okf-appearance-menu').contains(document.activeElement)"
    for name, spot in _MENU_SPOTS:
        x, y = spot(menu)
        page.mouse.click(x, y)
        expect(menu, f"a click on the {name} closed the menu").to_be_visible()
        expect(trigger).to_have_attribute("aria-expanded", "true")
        assert page.evaluate(inside), f"focus left the menu after a click on the {name}"
    # The dialog itself is a click focus target, never a Tab stop: Tab goes
    # on to the palette group's checked option.
    assert menu.get_attribute("tabindex") == "-1"
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement?.dataset.okfSet") == "theme"
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(trigger).to_be_focused()
    # A click outside the dialog still dismisses it.
    trigger.click()
    expect(menu).to_be_visible()
    x, y = _centre(page.locator("h1.okf-page__title, #okf-graph").first)
    page.mouse.click(x, y)
    expect(menu).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")
