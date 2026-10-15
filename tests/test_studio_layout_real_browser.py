"""Studio screen fit, measured in a real browser against the production server.

The workspace (Rendered / Source / Split) takes the article column whatever
the content length; running text shares one bounded lane with the title and
keeps the Ledger prose measure (~55ch); Related, Comments and Split are
admitted by the space that is really left. Every number here is measured CSS
geometry at a set viewport. None of this is browser zoom.
"""
from __future__ import annotations

import pytest

from browser_support import expect, launch_chromium, live_server, sync_playwright

pytestmark = pytest.mark.browser

# Ledger reading column (--doc-max / --okf-article-maxw) and Studio drawer.
LEDGER_COLUMN = 760
LEDGER_DRAWER = 320
PROSE_MEASURE = """() => { const p = document.querySelector('.okf-page__body > p');
  return p ? parseFloat(getComputedStyle(p).maxWidth) : null; }"""

SHORT = "---\ntype: Note\ntitle: Short\n---\n\n# Short\n\nTwo words.\n"
LONG_LINE = "okf-loom " + " ".join(f"--flag-{n}=value-{n}" for n in range(60))
RICH = f"""---
type: Reference
title: Rich reference
description: A page with running text, a wide table and a long code line.
---

# Rich reference

{"Running text that is long enough to reach the reading measure. " * 12}

## A section heading

{"More running text under a heading, again long enough to fill the lane. " * 8}

| Key | Value |
|---|---|
| a | b |

A paragraph keeps the two tables apart.

| {" | ".join(f"Column {n} heading" for n in range(14))} |
|{"---|" * 14}
| {" | ".join(f"cell value {n}" for n in range(14))} |

```bash
{LONG_LINE}
```

- A list item with running text long enough to reach the full reading measure.
- A second list item keeps the block multi-line.

> A quoted line that shares the same lane as the paragraphs around it.
"""


@pytest.fixture
def layout_base(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "short.md").write_text(SHORT, encoding="utf-8")
    (root / "rich.md").write_text(RICH, encoding="utf-8")
    with live_server(root) as base:
        yield base


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        chromium = launch_chromium(playwright)
        yield chromium
        chromium.close()


def _open(browser, base, path, width, height=900):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.console_errors = []
    page.on("console", lambda m: page.console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.console_errors.append(str(e)))
    page.goto(f"{base}/{path}", wait_until="load")
    page.wait_for_function(
        "() => window.okfLoomStudio && ['ready', 'error'].includes(window.okfLoomStudio.state.commentsStatus)")
    return page


GEOMETRY = """() => {
  const box = (sel) => { const n = document.querySelector(sel); if (!n || !n.getClientRects().length) return null;
    const r = n.getBoundingClientRect(); return {x: Math.round(r.left), w: Math.round(r.width), right: Math.round(r.right)}; };
  const panel = document.getElementById('okf-panel');
  return {
    article: box('.okf-page__main'), view: box('.okf-view') || box('.okf-page__body-col'),
    body: box('.okf-page__body'), source: box('.okf-source'),
    title: box('.okf-page__title'), h2: box('.okf-page__body > h2'), para: box('.okf-page__body > p'),
    related: (document.querySelector('.okf-page').dataset.okfRelated) || 'side',
    sidebar: box('.okf-page__sidebar'),
    rail: document.body.classList.contains('okf-panel-rail'),
    panelRole: panel.getAttribute('role'), panelModal: panel.getAttribute('aria-modal'),
    panel: panel.hidden ? null : box('#okf-panel'),
    mode: (document.querySelector('.okf-view') || {dataset: {}}).dataset.okfView || 'rendered',
    splitDisabled: document.querySelector('[data-mode="split"]').getAttribute('aria-disabled'),
    overflow: document.documentElement.scrollWidth - window.innerWidth,
  };
}"""


def _geometry(page):
    page.wait_for_timeout(250)  # rail padding transition (0.18s) + ResizeObserver frame
    return page.evaluate(GEOMETRY)


@pytest.mark.parametrize("view", ["rendered", "source", "split"])
def test_short_document_gets_the_whole_workspace(layout_base, browser, view):
    page = _open(browser, layout_base, "short" + ("" if view == "rendered" else f"?view={view}"), 1920, 1080)
    g = _geometry(page)
    assert g["mode"] == view, g
    # The article is the bounded content column (--okf-article-maxw): at
    # 1920 the shell track is wider, so the column caps at the Ledger 760px
    # reading column and the workspace still fills the WHOLE column whatever
    # the content length.
    assert g["article"]["w"] == LEDGER_COLUMN, g
    assert abs(g["view"]["w"] - g["article"]["w"]) <= 1, f"workspace shrink-wrapped its content: {g}"
    if view == "source":
        assert abs(g["source"]["w"] - g["article"]["w"]) <= 1, g
    if view == "split":
        assert g["body"]["w"] >= 300 and g["source"]["w"] >= 300, g
        assert g["body"]["w"] + g["source"]["w"] >= g["article"]["w"] - 16, g
    assert g["overflow"] <= 0 and not page.console_errors, (g, page.console_errors)


@pytest.mark.parametrize("width", [1920, 1280, 390])
def test_title_headings_and_prose_share_one_bounded_lane(layout_base, browser, width):
    """One bounded article column: title band, headings, prose AND wide
    blocks (table wrapper, code scrollport) all start on the same edge.
    Headings and wide blocks span the lane; running text keeps the Ledger
    prose measure (~55ch) and fills it, or the whole lane when the lane is
    narrower. No block overhangs the column."""
    page = _open(browser, layout_base, "rich", width)
    g = _geometry(page)
    measure = page.evaluate(PROSE_MEASURE)
    assert measure and measure < LEDGER_COLUMN, f"prose has no Ledger measure: {measure}"
    assert g["para"]["w"] <= g["article"]["w"], f"prose overflows the article column: {g}"
    assert abs(g["title"]["x"] - g["para"]["x"]) <= 1, f"title and prose start on different edges: {g}"
    assert abs(g["h2"]["x"] - g["para"]["x"]) <= 1, f"heading and prose start on different edges: {g}"
    # Long running text reaches its measure: the lane where it fits, else the
    # ~55ch prose measure (never an arbitrary narrower strip).
    assert abs(g["para"]["w"] - min(g["h2"]["w"], measure)) <= 1, f"prose misses its measure: {g} {measure}"
    # Wide blocks share the lane edges (the whole point of the single
    # column) instead of overhanging the text on either side.
    wide = page.evaluate("""() => {
      const f = [...document.querySelectorAll('.okf-page__body .okf-tablewrap')].pop();
      const r = f.getBoundingClientRect();
      return {left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width)};
    }""")
    assert abs(wide["left"] - g["para"]["x"]) <= 2, (wide, g)
    assert abs(wide["right"] - g["h2"]["right"]) <= 2, (wide, g)
    assert g["overflow"] <= 0, g


def test_panel_back_control_looks_clickable_and_responds(layout_base, browser):
    """The rail's '← Studio' control must read as a button at rest — the
    Ledger outlined button: a visible 1px --btn-border outline on a
    transparent ground, a 32px control height, pointer cursor, labelled for
    assistive tech — and must visibly respond on hover and press.
    Interaction semantics (activation, Escape, focus restoration) are
    covered by the shell and phase4 behavior tests; this pins the
    affordance."""
    page = _open(browser, layout_base, "rich", 1280)
    # Reach Comments the way a user does — Studio launcher, then the nested
    # Comments control. The back control only exists after a real Studio →
    # child step; a panel opened directly has no return target and must not
    # offer one (pinned by the visibility test below).
    page.locator(".okf-studio-launcher").click()
    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=False).click()
    page.wait_for_function("() => document.body.classList.contains('okf-panel-rail')")
    back = page.locator(".okf-panel__back")
    expect(back).to_be_visible()
    assert back.get_attribute("aria-label") == "Back to Studio"

    def bg_color():
        return page.evaluate(
            "() => getComputedStyle(document.querySelector('.okf-panel__back')).backgroundColor")

    rest = page.evaluate(
        """() => { const s = getComputedStyle(document.querySelector('.okf-panel__back'));
          return {cursor: s.cursor, bg: s.backgroundColor, borderColor: s.borderTopColor,
                  borderStyle: s.borderTopStyle, border: s.borderTopWidth,
                  minHeight: parseFloat(s.minHeight)}; }""")
    assert rest["cursor"] == "pointer", rest
    # Ledger buttons are outlined, not filled: the outline is the affordance.
    assert rest["borderStyle"] == "solid" and rest["borderColor"] not in ("transparent", "rgba(0, 0, 0, 0)"), (
        f"back control has no visible outline at rest — reads as plain text: {rest}")
    assert rest["border"] == "1px" and rest["minHeight"] >= 32, rest

    at_rest = bg_color()
    back.hover()
    page.wait_for_timeout(220)  # 0.12s background transition + frame
    hovered = bg_color()
    assert hovered != at_rest, f"no hover feedback: {at_rest} == {hovered}"
    page.mouse.down()
    page.wait_for_timeout(160)
    pressed = bg_color()
    page.mouse.up()
    assert pressed != hovered, f"no pressed feedback: {hovered} == {pressed}"
    # The press completed the action: the panel returned to the Studio home,
    # where the back control hands over to the persistent launcher.
    page.wait_for_function("() => document.getElementById('okf-panel-title').textContent === 'Studio'")
    assert not page.console_errors, page.console_errors
    page.context.close()


@pytest.mark.parametrize("width", [1280, 1000])
def test_back_control_appears_only_with_a_studio_return_target(layout_base, browser, width):
    """'← Studio' is offered only when the panel was entered from Studio home.

    Hidden must mean absent — from paint, tab order AND the accessibility
    tree — never a visible no-op: a fresh Studio root, a directly opened
    child panel, and everything reached after a close have no return target
    and must not show the control at all. 1280 exercises the desktop rail,
    1000 the compact modal handoff.
    """
    page = _open(browser, layout_base, "rich", width, 900 if width >= 1280 else 800)
    back = page.locator(".okf-panel__back")
    back_role = page.get_by_role("button", name="Back to Studio")

    def assert_no_back():
        expect(back).to_be_hidden()
        expect(back_role).to_have_count(0)  # not exposed to assistive tech either

    # Direct initial child open, with no Studio step beforehand.
    page.evaluate("() => window.okfLoomStudio.openPanel('comments')")
    page.wait_for_function("() => document.getElementById('okf-panel-title').textContent === 'Comments'")
    assert_no_back()
    page.get_by_role("button", name="Close panel").click()
    page.wait_for_function("() => document.getElementById('okf-panel').hidden")

    # A fresh Studio root has no Back either.
    page.locator(".okf-studio-launcher").click()
    page.wait_for_function("() => document.getElementById('okf-panel-title').textContent === 'Studio'")
    assert_no_back()

    # A real Studio → child step creates the return target: Back appears,
    # returns home, and hides itself again.
    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=False).click()
    page.wait_for_function("() => document.getElementById('okf-panel-title').textContent === 'Comments'")
    expect(back).to_be_visible()
    assert back.get_attribute("aria-label") == "Back to Studio"
    back.click()
    page.wait_for_function("() => document.getElementById('okf-panel-title').textContent === 'Studio'")
    assert_no_back()

    # Closing and reopening invents no history: the reopened panel shows no
    # Back at its root, and once closed again even a direct child open —
    # after Studio WAS visited earlier in this page session — still has no
    # return target (no stale trigger leaks through close/reopen).
    page.get_by_role("button", name="Close panel").click()
    page.wait_for_function("() => document.getElementById('okf-panel').hidden")
    page.locator(".okf-studio-launcher").click()
    page.wait_for_function("() => document.getElementById('okf-panel-title').textContent === 'Studio'")
    assert_no_back()
    page.get_by_role("button", name="Close panel").click()
    page.wait_for_function("() => document.getElementById('okf-panel').hidden")
    page.evaluate("() => window.okfLoomStudio.openPanel('comments')")
    page.wait_for_function("() => document.getElementById('okf-panel-title').textContent === 'Comments'")
    assert_no_back()
    assert not page.console_errors, page.console_errors
    page.context.close()


def test_related_yields_then_comments_overlays_by_measured_space(layout_base, browser):
    # 1200: Comments fits beside the article only because Related yields.
    page = _open(browser, layout_base, "rich", 1200)
    closed = _geometry(page)
    # Ledger: the Related panel sits to the right of the reading column.
    assert closed["related"] == "side" and closed["sidebar"]["x"] >= closed["article"]["right"], closed
    page.evaluate("() => window.okfLoomStudio.openPanel('comments')")
    page.wait_for_function("() => document.body.classList.contains('okf-panel-rail')")
    opened = _geometry(page)
    assert opened["related"] == "below", opened
    assert opened["article"]["w"] >= 600, f"reading column squeezed before Related yielded: {opened}"
    assert opened["panel"]["w"] == LEDGER_DRAWER, f"Studio drawer is not the Ledger 320px rail: {opened}"
    assert opened["article"]["right"] <= opened["panel"]["x"] + 1, opened
    assert opened["panelRole"] == "complementary" and opened["panelModal"] is None, opened
    assert opened["overflow"] <= 0, opened
    masthead = page.evaluate("""() => { const r = (s) => document.querySelector(s).getBoundingClientRect();
      const brand = r('.okf-shell__brand'), nav = r('.okf-shell__nav');
      const items = [...document.querySelectorAll('.okf-shell__nav > *')].filter(n => n.getClientRects().length)
        .map(n => n.getBoundingClientRect());
      return {collide: brand.right > nav.left + 1 && brand.bottom > nav.top + 1 && nav.bottom > brand.top + 1,
        clipped: items.filter(i => i.width < 24 || i.right > r('.okf-topbar').right + 1).length}; }""")
    assert masthead == {"collide": False, "clipped": 0}, masthead
    page.evaluate("() => window.okfLoomStudio.closePanel()")
    page.wait_for_function("() => !document.body.classList.contains('okf-panel-rail')")
    restored = _geometry(page)
    assert restored["related"] == "side" and restored["article"] == closed["article"], (closed, restored)
    page.context.close()

    # 900: no safe side-by-side even with Related yielded (the 320px rail
    # would leave less than the 600px reading minimum), so Comments overlays
    # as a modal and the document keeps its geometry underneath.
    page = _open(browser, layout_base, "rich", 900, 800)
    closed = _geometry(page)
    page.evaluate("() => window.okfLoomStudio.openPanel('comments')")
    page.locator("#okf-panel").wait_for(state="visible")
    opened = _geometry(page)
    assert opened["rail"] is False and opened["panelRole"] == "dialog" and opened["panelModal"] == "true", opened
    assert opened["article"] == closed["article"] and opened["related"] == "side", (closed, opened)
    assert opened["overflow"] <= 0 and not page.console_errors, (opened, page.console_errors)


def test_split_is_admitted_by_workspace_width_and_returns_with_the_room(layout_base, browser):
    # 960: Split fits the closed workspace; the 320px Comments rail then
    # leaves a 600px column, too narrow for two readable panes.
    page = _open(browser, layout_base, "rich?view=split", 960, 800)
    g = _geometry(page)
    assert g["mode"] == "split" and g["splitDisabled"] is None, g
    assert g["body"]["w"] >= 300 and g["source"]["w"] >= 300, g

    page.evaluate("() => window.okfLoomStudio.openPanel('comments')")
    page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'rendered'")
    g = _geometry(page)
    assert g["rail"] is True and g["splitDisabled"] == "true", g
    assert g["article"]["w"] >= 600 and g["overflow"] <= 0, g
    expect(page.locator("#okf-annotation-status")).to_contain_text("Split paused")
    split = page.locator(".okf-viewswitch__btn[data-mode='split']")
    assert "wider workspace" in (split.get_attribute("title") or "")
    # Still a reachable, explained choice — never a silent two-pane crush.
    split.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#okf-annotation-status")).to_contain_text("Split needs a wider workspace")
    assert _geometry(page)["mode"] == "rendered"

    page.evaluate("() => window.okfLoomStudio.closePanel()")
    page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'split'")
    g = _geometry(page)
    assert g["splitDisabled"] is None and g["body"]["w"] >= 300 and g["source"]["w"] >= 300, g

    # An explicit choice is not overridden when the room comes back.
    page.evaluate("() => window.okfLoomStudio.openPanel('comments')")
    page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'rendered'")
    page.locator(".okf-viewswitch__btn[data-mode='source']").click()
    page.evaluate("() => window.okfLoomStudio.closePanel()")
    page.wait_for_function("() => !document.body.classList.contains('okf-panel-rail')")
    assert _geometry(page)["mode"] == "source"
    assert not page.console_errors, page.console_errors


# --- Reading Focus workspace + scroll-aware document view controls --------

BAR_STACK = """() => {
  const rect = (sel) => { const n = document.querySelector(sel); if (!n || !n.getClientRects().length) return null;
    const r = n.getBoundingClientRect(); return {x: Math.round(r.left), y: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height), bottom: Math.round(r.bottom)}; };
  const style = getComputedStyle(document.documentElement);
  return {
    focus: document.body.classList.contains('okf-focus-view'),
    mode: (document.querySelector('.okf-view') || {dataset: {}}).dataset.okfView || 'rendered',
    viewport: {w: innerWidth, h: innerHeight},
    clientW: document.documentElement.clientWidth,
    topbar: rect('.okf-topbar'), bar: rect('.okf-concept-viewbar'),
    page: rect('.okf-page'), article: rect('.okf-page__main'),
    bodyPane: rect('.okf-page__body'), sourcePane: rect('.okf-source'),
    sidebarShown: !!(document.querySelector('.okf-page__sidebar') || {getClientRects: () => []}).getClientRects().length,
    viewbarTopVar: style.getPropertyValue('--okf-viewbar-top').trim(),
    viewbarHVar: style.getPropertyValue('--okf-viewbar-h').trim(),
    occlusionVar: style.getPropertyValue('--okf-document-occlusion').trim(),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
}"""


def _assert_view_controls_hit(page):
    """Every painted view control must receive pointer input, not just exist."""
    controls = page.locator(".okf-concept-viewbar .okf-viewswitch__btn")
    assert controls.count() >= 5
    for control in controls.all():
        if not control.is_visible():
            continue  # Wrap is absent in Rendered; it is checked again in Source.
        assert control.evaluate("""node => {
          const r = node.getBoundingClientRect();
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return r.top >= 0 && r.bottom <= innerHeight && !!hit && node.contains(hit);
        }"""), control.inner_text()


def _reveal_phone_viewbar_after_scroll(page):
    """Down hides the tabs by transform; a small upward scroll reveals them."""
    page.wait_for_function("() => document.documentElement.dataset.okfChrome === 'compact'")
    page.wait_for_function("""() => !document.getAnimations().some(a =>
      a.transitionProperty === 'transform' && a.playState === 'running')""")
    hidden = page.evaluate("""() => {
      const bar = document.querySelector('.okf-concept-viewbar');
      const tabs = bar.querySelector('.okf-viewswitch');
      const jump = bar.querySelector('.okf-outline-jump');
      const top = document.querySelector('.okf-topbar');
      return {shift: new DOMMatrixReadOnly(getComputedStyle(bar).transform).m42,
        tabsBottom: tabs.getBoundingClientRect().bottom,
        topBottom: top.getClientRects().length ? top.getBoundingClientRect().bottom : 0,
        jumpTop: jump && jump.getClientRects().length ? jump.getBoundingClientRect().top : null};
    }""")
    assert hidden["shift"] < 0 and hidden["tabsBottom"] <= 1 and hidden["topBottom"] <= 1, hidden
    if hidden["jumpTop"] is not None:
        assert 0 <= hidden["jumpTop"] <= 8, hidden  # Only Sections stays pinned.
    page.evaluate("() => scrollBy(0, -50)")
    page.wait_for_function("() => document.documentElement.dataset.okfChrome === 'revealed'")
    page.wait_for_function("""() => !document.getAnimations().some(a =>
      a.transitionProperty === 'transform' && a.playState === 'running')""")
    g = page.evaluate(BAR_STACK)
    if g["topbar"] is None:  # Reading Focus has no top bar.
        assert g["bar"]["y"] == 0, g
    else:
        assert g["topbar"]["y"] == 0, g
        assert abs(g["bar"]["y"] - g["topbar"]["bottom"]) <= 1, g
    _assert_view_controls_hit(page)


@pytest.mark.parametrize("width", [1600, 1920])
def test_focus_view_gives_the_workspace_the_available_width(layout_base, browser, width):
    """Reading Focus is the intentional exception to the bounded article
    column: Rendered text and both Split panes take the full available width
    (viewport minus the page gutters), never the former ~76ch page cap or the
    1120px article cap, and no rail track survives."""
    page = _open(browser, layout_base, "rich?view=split&focus=1", width, 900)
    g = page.evaluate(BAR_STACK)
    assert g["focus"] is True and g["mode"] == "split", g
    assert abs(g["page"]["w"] - g["clientW"]) <= 1, f"focus page kept a narrow cap: {g}"
    # Modest page gutters, then the article (and the split panes) take the rest.
    assert 40 <= g["page"]["w"] - g["article"]["w"] <= 64, g
    assert g["article"]["w"] >= g["clientW"] - 64, g
    assert g["sidebarShown"] is False, g
    assert g["bodyPane"]["w"] >= 300 and g["sourcePane"]["w"] >= 300, g
    assert g["bodyPane"]["w"] + g["sourcePane"]["w"] >= g["article"]["w"] - 40, g
    assert g["overflow"] <= 0, g
    page.context.close()


def test_focus_rendered_expands_and_exits_cleanly(layout_base, browser):
    page = _open(browser, layout_base, "rich?focus=1", 1600, 900)
    g = page.evaluate(BAR_STACK)
    assert g["article"]["w"] >= g["clientW"] - 64, f"focus rendered kept a narrow cap: {g}"
    focus_btn = page.get_by_role("button", name="Focus view")
    focus_btn.click()
    page.wait_for_function("() => !document.body.classList.contains('okf-focus-view')")
    assert "focus=" not in page.url
    g = page.evaluate(BAR_STACK)
    # Exiting Focus restores the bounded normal-mode article column.
    assert g["focus"] is False and g["article"]["w"] == LEDGER_COLUMN, g
    assert not page.console_errors, page.console_errors
    page.context.close()


def test_escape_exits_reading_focus_and_yields_to_higher_ownership(layout_base, browser):
    """U5: Escape exits Reading Focus through the canonical writer — body
    class, aria-pressed, and the ?focus URL all return to normal view — and
    restores focus to the connected Focus control. A higher-priority overlay
    (command palette, slide-over drawer, Sections popover) takes the first
    Escape WITHOUT also dropping Reading Focus."""
    # 1) Focus alone: one Escape exits, focus lands on the Focus control.
    page = _open(browser, layout_base, "rich?focus=1", 1280)
    focus_btn = page.get_by_role("button", name="Focus view")
    assert focus_btn.get_attribute("aria-pressed") == "true"
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.body.classList.contains('okf-focus-view')")
    assert "focus=" not in page.url
    assert focus_btn.get_attribute("aria-pressed") == "false"
    expect(focus_btn).to_be_focused()
    page.context.close()

    # 2) Command palette open: the first Escape closes only the palette.
    page = _open(browser, layout_base, "rich?focus=1", 1280)
    page.keyboard.press("Control+k")
    palette = page.locator(".okf-palette-overlay")
    expect(palette).to_be_visible()
    page.keyboard.press("Escape")
    expect(palette).to_be_hidden()
    assert page.evaluate("document.body.classList.contains('okf-focus-view')"), page.url
    assert "focus=1" in page.url
    assert page.get_by_role("button", name="Focus view").get_attribute("aria-pressed") == "true"
    # The next Escape — with no overlay left — exits Reading Focus.
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.body.classList.contains('okf-focus-view')")
    page.context.close()

    # 3) Slide-over drawer open (via the palette: the shell launcher is
    #    intentionally hidden in Reading Focus): Escape closes the drawer only.
    page = _open(browser, layout_base, "rich?focus=1", 1280)
    page.keyboard.press("Control+k")
    page.locator(".okf-palette__input").fill("Open Comments panel")
    page.keyboard.press("Enter")
    panel = page.locator("#okf-panel")
    expect(panel).to_be_visible()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    assert page.evaluate("document.body.classList.contains('okf-focus-view')"), page.url
    assert "focus=1" in page.url
    page.context.close()

    # 4) Sections popover open: its own Escape owner closes it and restores
    #    the toggle; Reading Focus stays until a second Escape.
    page = _open(browser, layout_base, "rich?focus=1", 1280)
    toggle = page.locator(".okf-outline-jump__toggle")
    expect(toggle).to_be_visible()
    toggle.click()
    popover = page.locator("#okf-outline-jump-panel")
    expect(popover).to_be_visible()
    page.keyboard.press("Escape")
    expect(popover).to_be_hidden()
    expect(toggle).to_be_focused()
    assert page.evaluate("document.body.classList.contains('okf-focus-view')"), page.url
    assert "focus=1" in page.url
    page.context.close()


def _focus_blocks(page):
    """Bounding boxes of representative RENDERED blocks, not just containers.

    The review that returned the first Focus fix to FAIL showed why the outer
    article width alone is not acceptance evidence: wiki.css caps the rendered
    descendants through --okf-lane, so the article can be wide while every
    block inside it is still bounded at the normal reading column."""
    return page.evaluate("""() => {
      const box = (n) => { if (!n || !n.getClientRects().length) return null;
        const r = n.getBoundingClientRect();
        return {x: Math.round(r.left), w: Math.round(r.width), right: Math.round(r.right)}; };
      const q = (sel) => box(document.querySelector(sel));
      const wide = [...document.querySelectorAll('.okf-page__body .okf-tablewrap-frame')]
        .pop() || [...document.querySelectorAll('.okf-page__body .okf-tablewrap')].pop();
      const pane = document.querySelector('.okf-view[data-okf-view="split"] > .okf-page__body');
      return {
        article: q('.okf-page__main'), title: q('.okf-page__title'),
        para: q('.okf-page__body > p'), h2: q('.okf-page__body > h2'),
        list: q('.okf-page__body > ul'), quote: q('.okf-page__body > blockquote'),
        code: q('.okf-page__body .okf-content-scrollport'), table: box(wide),
        splitPane: box(pane), splitPara: pane ? box(pane.querySelector('p')) : null,
        splitScrolls: pane ? pane.scrollHeight > pane.clientHeight + 1 : null,
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    }""")


def _assert_focus_block_spans_workspace(block, article, label):
    """A focus wide block (code, table) must span the available workspace
    width — the article minus its modest reading gutters on both sides — not
    the normal 760px reading column."""
    inset = block["x"] - article["x"]
    assert 0 <= inset <= 32, f"{label} lost the gutter alignment: {block} vs {article}"
    expected = article["w"] - 2 * inset
    assert expected > LEDGER_COLUMN, f"viewport is not beyond the normal column: {article}"
    assert abs(block["w"] - expected) <= 2, f"{label} kept an inner cap: {block} vs {article}"


def test_focus_rendered_blocks_fill_the_expanded_workspace(layout_base, browser):
    """Reading Focus must widen the CONTENT, not only its container: the
    title band and ordinary code/table blocks take the whole available
    workspace with the existing gutters at a desktop width beyond the 760px
    column, while running text (paragraphs, list items, quotes) keeps the
    Ledger ~55ch prose measure on the same left edge. Focus Split keeps its
    pane geometry, and the rendered pane's own text sits at the pane edge
    with the pane as the real scroll surface."""
    page = _open(browser, layout_base, "rich", 1600, 900)
    normal = _focus_blocks(page)
    measure = page.evaluate(PROSE_MEASURE)
    # Normal mode keeps the bounded lane: no block escapes the 760px column.
    for label in ("para", "h2", "list", "quote", "code", "table"):
        assert normal[label]["w"] <= LEDGER_COLUMN, f"normal {label} exceeds the bounded lane: {normal}"

    page = _open(browser, layout_base, "rich?focus=1", 1600, 900)
    focus = _focus_blocks(page)
    for label in ("code", "table"):
        _assert_focus_block_spans_workspace(focus[label], focus["article"], f"focus {label}")
    assert focus["title"]["w"] >= focus["article"]["w"] - 64, f"focus title band kept a cap: {focus}"
    for label in ("para", "list", "quote"):
        # Running text keeps the prose measure and fills it (long lines).
        assert abs(focus[label]["w"] - measure) <= 2, f"focus {label} lost the prose measure: {focus}"
        assert abs(focus[label]["x"] - focus["code"]["x"]) <= 2, f"focus {label} left edge drifted: {focus}"
    assert abs(focus["title"]["x"] - focus["para"]["x"]) <= 2, focus
    assert focus["overflow"] <= 0, focus

    # Focus Split at 1600: panes keep the ~half/half geometry of the expanded
    # article; the rendered pane's text starts at the pane edge and keeps the
    # prose measure inside it, and the pane — not a blank clamp — owns the
    # scrolling.
    page = _open(browser, layout_base, "rich?view=split&focus=1", 1600, 900)
    split = _focus_blocks(page)
    assert split["splitPane"]["w"] >= 700, split
    assert split["splitPane"]["w"] <= split["article"]["w"] / 2 + 8, split
    assert abs(split["splitPara"]["x"] - split["splitPane"]["x"]) <= 1, split
    # 12px = --okf-space-3 pane padding when the pane is narrower than the measure.
    assert abs(split["splitPara"]["w"] - min(split["splitPane"]["w"] - 12, measure)) <= 2, \
        f"split rendered text misses its measure inside the pane: {split}"
    assert split["splitScrolls"], f"split pane lost its usable scrolling height: {split}"
    assert split["overflow"] <= 0, split
    page.context.close()


HEADINGLESS = "---\ntype: Note\ntitle: Plain ledger\ndescription: A long concept whose body has no headings at all.\n---\n\n" + "\n\n".join(
    f"Ledger entry {n}: " + "running text without a single heading, tall enough to scroll far past the wrapped shell. " * 3
    for n in range(1, 25)
) + "\n"


@pytest.fixture
def headingless_base(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.md").write_text("# Home\n", encoding="utf-8")
    (root / "plain.md").write_text(HEADINGLESS, encoding="utf-8")
    with live_server(root) as base:
        yield base


def test_headingless_concept_publishes_chrome_geometry_and_keeps_toolbar_operable(headingless_base, browser):
    """A document without body headings still gets its stable (hidden, empty)
    [data-okf-document-outline] host (F8: the outline can be filled in later by
    a live patch without a reload), and the measured sticky-chrome variables
    (--okf-viewbar-top/-h) must still be published — with only the nominal
    token fallback, a wrapped shell paints over the pinned viewbar. Prove the
    geometry, then prove the phone tabs scroll away and return on scroll-up,
    with real clicks at a deep scroll, in Focus and after exiting it."""
    page = _open(browser, headingless_base, "plain", 390, 844)
    assert page.evaluate("""() => {
        const host = document.querySelector('[data-okf-document-outline]');
        return !!host && host.hidden && host.classList.contains('okf-onthispage')
            && host.hasAttribute('data-okf-outline-empty') && !host.querySelector('a');
    }"""), "headingless page must carry the stable hidden empty outline host"
    page.wait_for_timeout(250)
    g = page.evaluate(BAR_STACK)
    assert g["viewbarTopVar"], f"chrome geometry never published without an outline: {g}"
    assert g["topbar"]["h"] >= 60, f"fixture shell is not wrapped at 390px: {g}"
    assert abs(g["bar"]["y"] - float(g["viewbarTopVar"].replace("px", ""))) <= 1, g

    page.evaluate("() => scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_timeout(150)
    _reveal_phone_viewbar_after_scroll(page)
    g = page.evaluate(BAR_STACK)
    assert g["bar"]["y"] >= g["topbar"]["bottom"] - 1, f"revealed viewbar pinned under the wrapped shell: {g}"
    assert g["bar"]["bottom"] <= g["viewport"]["h"], f"viewbar scrolled out of the viewport: {g}"
    assert abs(g["bar"]["y"] - float(g["viewbarTopVar"].replace("px", ""))) <= 1, g
    assert abs(float(g["viewbarHVar"].replace("px", "")) - g["bar"]["h"]) <= 1, g

    # Real clicks on the revealed bar while still deep in the document.
    # Split is aria-disabled at this width but still explains a real pointer press.
    split = page.locator('.okf-viewswitch__btn[data-mode="split"]').bounding_box()
    page.mouse.click(split["x"] + split["width"] / 2, split["y"] + split["height"] / 2)
    expect(page.locator("#okf-annotation-status")).to_contain_text("Split needs a wider workspace")
    assert page.evaluate(BAR_STACK)["mode"] == "rendered"
    page.locator('.okf-viewswitch__btn[data-mode="source"]').click()
    page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'source'")
    wrap = page.get_by_role("button", name="Wrap lines")
    expect(wrap).to_be_visible()
    _assert_view_controls_hit(page)
    wrap.click()
    expect(wrap).to_have_attribute("aria-pressed", "false")

    page.get_by_role("button", name="Focus view").click()
    page.wait_for_function("() => document.body.classList.contains('okf-focus-view')")
    page.evaluate("() => scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_timeout(150)
    g = page.evaluate(BAR_STACK)
    assert g["topbar"] is None and g["bar"]["y"] == 0, f"focus viewbar not pinned at the top: {g}"

    # Exiting Focus restores the MEASURED stack (not the nominal token) and
    # the mode controls keep working.
    page.get_by_role("button", name="Focus view").click()
    page.wait_for_function("() => !document.body.classList.contains('okf-focus-view')")
    page.wait_for_timeout(250)
    g = page.evaluate(BAR_STACK)
    assert g["topbar"]["h"] >= 60, f"shell did not re-wrap after exiting focus: {g}"
    assert g["bar"]["y"] >= g["topbar"]["bottom"] - 1, f"viewbar under the re-wrapped shell: {g}"
    assert abs(g["bar"]["y"] - float(g["viewbarTopVar"].replace("px", ""))) <= 1, g
    page.locator('.okf-viewswitch__btn[data-mode="rendered"]').click()
    page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'rendered'")
    assert g["overflow"] <= 0, g
    assert not page.console_errors, page.console_errors
    page.context.close()


@pytest.mark.parametrize("width", [1600, 390])
def test_view_controls_stay_visible_and_operable_while_scrolled(layout_base, browser, width):
    """Desktop keeps the toolbar pinned; phone tabs hide on scroll-down and
    return on scroll-up. Once revealed every control is really clickable at
    depth, below the measured shell or at the top in Reading Focus."""
    page = _open(browser, layout_base, "rich", width, 900)
    page.wait_for_timeout(250)
    page.evaluate("() => scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_timeout(150)
    if width <= 640:
        _reveal_phone_viewbar_after_scroll(page)
    g = page.evaluate(BAR_STACK)
    assert g["bar"], g
    assert g["bar"]["y"] >= 0 and g["bar"]["bottom"] <= g["viewport"]["h"], \
        f"viewbar scrolled out of the viewport in normal view: {g}"
    assert g["bar"]["y"] >= g["topbar"]["bottom"] - 1, f"viewbar overlapped the shell: {g}"
    assert abs(g["bar"]["y"] - float(g["viewbarTopVar"].replace("px", ""))) <= 1, g
    assert abs(float(g["viewbarHVar"].replace("px", "")) - g["bar"]["h"]) <= 1, g

    # Hit-testing plus real Playwright clicks prove the pinned/revealed controls
    # are operable, not merely present in the DOM.
    _assert_view_controls_hit(page)
    if width >= 900:
        page.locator('.okf-viewswitch__btn[data-mode="split"]').click()
        page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'split'")
        wrap = page.get_by_role("button", name="Wrap lines")
        expect(wrap).to_be_visible()
    else:
        # aria-disabled Split still explains a real pointer press, without a forced click.
        split = page.locator('.okf-viewswitch__btn[data-mode="split"]').bounding_box()
        page.mouse.click(split["x"] + split["width"] / 2, split["y"] + split["height"] / 2)
        expect(page.locator("#okf-annotation-status")).to_contain_text("Split needs a wider workspace")
        assert page.evaluate(BAR_STACK)["mode"] == "rendered"
        page.locator('.okf-viewswitch__btn[data-mode="source"]').click()
        page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'source'")
        _assert_view_controls_hit(page)
        wrap = page.get_by_role("button", name="Wrap lines")
        expect(wrap).to_be_visible()
        wrap.click()
        expect(wrap).to_have_attribute("aria-pressed", "false")
        page.locator('.okf-viewswitch__btn[data-mode="rendered"]').click()
        page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'rendered'")
        page.locator('.okf-viewswitch__btn[data-mode="source"]').click()
        page.wait_for_function("() => document.querySelector('.okf-view').dataset.okfView === 'source'")

    page.get_by_role("button", name="Focus view").click()
    page.wait_for_function("() => document.body.classList.contains('okf-focus-view')")
    g = page.evaluate(BAR_STACK)
    assert g["topbar"] is None, f"shell still visible in focus: {g}"
    assert g["bar"]["y"] == 0, f"viewbar not pinned at the top in focus: {g}"
    assert float(g["viewbarTopVar"].replace("px", "")) == 0, g

    page.evaluate("() => scrollTo(0, Math.round(document.documentElement.scrollHeight / 2))")
    page.wait_for_timeout(150)
    if width <= 640:
        _reveal_phone_viewbar_after_scroll(page)
    g = page.evaluate(BAR_STACK)
    assert g["bar"]["y"] == 0 and g["bar"]["bottom"] <= g["viewport"]["h"], \
        f"viewbar did not stay pinned mid-scroll in focus: {g}"
    page.get_by_role("button", name="Focus view").click()
    page.wait_for_function("() => !document.body.classList.contains('okf-focus-view')")
    assert g["overflow"] <= 0, g
    assert not page.console_errors, page.console_errors
    page.context.close()


@pytest.mark.parametrize("suffix, focus", [("", False), ("?focus=1", True)])
def test_hash_landing_clears_the_sticky_chrome(layout_base, browser, suffix, focus):
    """Heading/hash jumps must land below the whole sticky stack — topbar AND
    the pinned document viewbar — never underneath it."""
    page = _open(browser, layout_base, "rich" + suffix, 1600, 900)
    page.wait_for_timeout(250)
    target_id = page.evaluate("() => document.querySelector('.okf-page__body h2[id]').id")
    page.evaluate("(id) => { location.hash = id; }", target_id)
    page.wait_for_timeout(150)
    landing = page.evaluate("""() => {
      const h = document.querySelector('.okf-page__body h2[id]');
      const bar = document.querySelector('.okf-concept-viewbar');
      return {headingTop: Math.round(h.getBoundingClientRect().top),
              chromeBottom: Math.round(Math.max(bar.getBoundingClientRect().bottom, 0)),
              occlusionVar: getComputedStyle(document.documentElement)
                .getPropertyValue('--okf-document-occlusion').trim()};
    }""")
    assert landing["headingTop"] >= landing["chromeBottom"], landing
    assert float(landing["occlusionVar"].replace("px", "")) >= landing["chromeBottom"] + 16, landing
    page.context.close()


def test_source_soft_wraps_by_default_with_a_keyboard_wrap_control(layout_base, browser):
    page = _open(browser, layout_base, "rich", 1280)
    wrap = page.get_by_role("button", name="Wrap lines")
    expect(wrap).to_be_hidden()  # no Source pane on screen, no control
    page.locator(".okf-viewswitch__btn[data-mode='source']").click()
    page.wait_for_function("() => document.querySelector('.okf-source').dataset.loaded === '1'")
    expect(wrap).to_be_visible()
    expect(wrap).to_have_attribute("aria-pressed", "true")

    source = """() => { const s = document.querySelector('.okf-source'); return {
      ws: getComputedStyle(s).whiteSpace, scrolls: s.scrollWidth > s.clientWidth + 1, tab: s.getAttribute('tabindex'),
      role: s.getAttribute('role'), label: s.getAttribute('aria-label'), left: s.scrollLeft,
      overflow: document.documentElement.scrollWidth - window.innerWidth }; }"""
    wrapped = page.evaluate(source)
    assert wrapped["ws"] == "pre-wrap" and not wrapped["scrolls"] and wrapped["tab"] is None, wrapped

    wrap.focus()
    page.keyboard.press("Enter")
    expect(wrap).to_have_attribute("aria-pressed", "false")
    page.wait_for_function("() => document.querySelector('.okf-source').getAttribute('tabindex') === '0'")
    unwrapped = page.evaluate(source)
    assert unwrapped["ws"] == "pre" and unwrapped["scrolls"] and unwrapped["role"] == "region", unwrapped
    assert "Scroll" in unwrapped["label"] and unwrapped["overflow"] <= 0, unwrapped
    # Local scrolling is reachable from the keyboard alone.
    page.keyboard.press("Tab")
    for _ in range(12):
        if page.evaluate("() => document.activeElement === document.querySelector('.okf-source')"):
            break
        page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement === document.querySelector('.okf-source')")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => document.querySelector('.okf-source').scrollLeft > 0")

    # The preference survives a reload; turning it back on removes the stop.
    page.reload(wait_until="load")
    page.wait_for_function("() => { const s = document.querySelector('.okf-source');"
                           " return s && s.dataset.loaded === '1' && s.getAttribute('tabindex') === '0'; }")
    wrap = page.get_by_role("button", name="Wrap lines")
    expect(wrap).to_have_attribute("aria-pressed", "false")
    wrap.click()
    page.wait_for_function("() => document.querySelector('.okf-source').getAttribute('tabindex') === null")
    assert page.evaluate(source)["ws"] == "pre-wrap"
    assert not page.console_errors, page.console_errors


def test_rendered_code_and_tables_scroll_locally_only_when_they_must(layout_base, browser):
    page = _open(browser, layout_base, "rich", 1280)
    page.wait_for_function("() => document.querySelectorAll('.okf-page__body .okf-tablewrap').length === 2")
    page.wait_for_timeout(300)
    regions = page.evaluate("""() => {
      const info = (n) => ({scrolls: n.scrollWidth > n.clientWidth + 1, tab: n.getAttribute('tabindex'),
        role: n.getAttribute('role'), label: n.getAttribute('aria-label')});
      const tables = [...document.querySelectorAll('.okf-page__body .okf-tablewrap')].map(info);
      const code = document.querySelector('.okf-page__body .okf-content-scrollport');
      const pre = code.querySelector('pre');
      const cell = document.querySelector('.okf-page__body .okf-tablewrap:last-of-type td') ||
        [...document.querySelectorAll('.okf-page__body td')].pop();
      return {small: tables[0], wide: tables[1], code: info(code), preWs: getComputedStyle(pre).whiteSpace,
        cellFont: parseFloat(getComputedStyle(cell).fontSize),
        overflow: document.documentElement.scrollWidth - window.innerWidth}; }""")
    # A table that fits is not a scroll region or a tab stop (the header
    # resize grip used to overhang and fake 3px of overflow).
    assert regions["small"] == {"scrolls": False, "tab": None, "role": None, "label": None}, regions
    for key in ("wide", "code"):
        assert regions[key]["scrolls"] and regions[key]["tab"] == "0" and regions[key]["role"] == "region", regions
        assert regions[key]["label"], regions
    assert regions["preWs"] == "pre", regions          # formatting kept, not wrapped
    assert regions["cellFont"] >= 12.5, regions        # scrolled, not scaled down (Ledger cell 12.5px)
    assert regions["overflow"] <= 0, regions
    port = page.locator(".okf-page__body .okf-content-scrollport").first
    port.focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    page.wait_for_function(
        "() => document.querySelector('.okf-page__body .okf-content-scrollport').scrollLeft > 0")


# --- single-file Index: every entry reachable -----------------------------

@pytest.fixture
def single_file(tmp_path):
    from scripts.okf_loom.model import Bundle
    from scripts.okf_loom.render import build_site

    root = tmp_path / "many"
    (root / "area").mkdir(parents=True)
    (root / "index.md").write_text("# Many\n", encoding="utf-8")
    for n in range(40):
        body = f"# Concept {n:02d}\n\nBody of concept {n:02d}.\n"
        if n == 0:
            # One real internal edge so the no-JS relationships fallback has
            # a resolved relationship to show.
            body += "\nRelated: [Concept 01](c01.md)\n"
        (root / "area" / f"c{n:02d}.md").write_text(
            f"---\ntype: Note\ntitle: Concept {n:02d}\ndescription: Entry number {n:02d} of forty.\n---\n\n"
            f"{body}", encoding="utf-8")
    out = tmp_path / "single"
    build_site(Bundle.load(root), out, target="single-file")
    return (out / "index.html").as_uri()


def test_single_file_nojs_notice_is_honest_and_fallbacks_stay_readable(single_file, browser):
    """Single-file output without JavaScript: the no-JS notice is visible and
    says honestly WHAT needs JavaScript (bodies, graph, search) and what
    stays readable (concept list, resolved relationships); the JS-only tools
    are hidden; the fallback sections are actually readable. With JavaScript
    the notice stands down."""
    nojs = browser.new_context(viewport={"width": 1280, "height": 900}, java_script_enabled=False)
    page = nojs.new_page()
    page.goto(single_file, wait_until="load")
    notice = page.locator(".okf-single-nojs")
    expect(notice).to_be_visible()
    text = notice.inner_text()
    assert "JavaScript is off" in text
    assert "Concept bodies" in text and "remain readable" in text, text
    # What remains readable really is readable.
    assert page.locator("#okf-single-index .okf-single-index__list li").count() == 40
    expect(page.locator("#okf-single-graph-fallback")).to_be_visible()
    assert page.locator("#okf-single-graph-fallback li").count() > 0
    # JS-only controls stand down.
    assert not page.locator(".okf-single-graph-tools").is_visible()
    # Concept bodies are embedded data (not server-rendered HTML): the
    # notice's claim that they need JavaScript must be literally true.
    assert page.locator("#detail-body").inner_text().strip() == ""
    nojs.close()

    js = browser.new_context(viewport={"width": 1280, "height": 900})
    page = js.new_page()
    page.goto(single_file, wait_until="load")
    page.wait_for_function("() => document.body.dataset.okfSingleDestination === 'index'")
    expect(page.locator(".okf-single-nojs")).to_be_hidden()
    js.close()


@pytest.mark.parametrize("width,height,touch", [(1920, 900, False), (1280, 800, False), (390, 844, True)])
def test_single_file_index_reaches_and_opens_every_entry(single_file, browser, width, height, touch):
    context = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(single_file, wait_until="load")
    page.wait_for_function("() => document.body.dataset.okfSingleDestination === 'index'")
    entries = page.locator("#okf-single-index a[data-okf-single-concept]")
    assert entries.count() == 40
    assert page.evaluate("() => document.documentElement.scrollHeight") > height + 200  # genuinely below the fold

    # Wheel reaches the end of the list (scrollY used to stay at 0).
    page.mouse.move(width / 2, height / 2)
    for _ in range(40):
        page.mouse.wheel(0, 1200)
    page.wait_for_function(
        "() => Math.ceil(window.scrollY + window.innerHeight) >= document.documentElement.scrollHeight - 2")
    expect(entries.last).to_be_in_viewport()
    page.evaluate("() => window.scrollTo(0, 0)")

    # Keyboard alone walks every entry in order, each scrolled into view.
    entries.first.focus()
    seen = []
    for index in range(40):
        state = page.evaluate("""() => { const a = document.activeElement, r = a.getBoundingClientRect();
          return {id: a.getAttribute('data-okf-single-concept'), visible: r.top >= 0 && r.bottom <= window.innerHeight + 1}; }""")
        assert state["visible"], (index, state)
        seen.append(state["id"])
        if index < 39:
            page.keyboard.press("Tab")
    assert seen == [f"area/c{n:02d}" for n in range(40)], seen

    # Enter on the last entry opens that concept in place and moves focus to it.
    href = entries.last.get_attribute("href")
    assert href == "?selected=area%2Fc39#detail-content", href
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.body.dataset.okfSingleDestination === 'concept'")
    expect(page.locator("#detail-body")).to_contain_text("Body of concept 39.")
    expect(page.locator("#okf-single-index")).to_be_hidden()
    assert page.evaluate("() => document.activeElement && document.activeElement.id") == "detail-content"
    assert "selected=area%2Fc39" in page.url
    # The graph destination keeps its own viewport-bound workspace.
    if width >= 900:
        assert page.evaluate("() => getComputedStyle(document.body).overflowY") == "hidden"

    # Back returns to the list, which still scrolls.
    page.go_back()
    page.wait_for_function("() => document.body.dataset.okfSingleDestination === 'index'")
    expect(page.locator("#okf-single-index")).to_be_visible()
    page.keyboard.press("Control+End") if not touch else page.evaluate(
        "() => window.scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_function("() => window.scrollY > 0")

    # The href is a real deep link on its own (new tab, reload, no in-place handler).
    fresh = context.new_page()
    fresh.goto(single_file.split("#")[0] + "?selected=area%2Fc07#detail-content", wait_until="load")
    fresh.wait_for_function("() => document.body.dataset.okfSingleDestination === 'concept'")
    expect(fresh.locator("#detail-body")).to_contain_text("Body of concept 07.")
    assert not errors, errors
    context.close()
