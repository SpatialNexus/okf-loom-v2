"""Browser proof for graph composite widgets and synchronized interaction state."""
from __future__ import annotations

import re

import pytest

from browser_support import expect, reveal_control

from test_viewer_browser import docs_server_url, page, server_url  # noqa: E402

pytestmark = pytest.mark.browser


def _open(page, server_url: str, width: int = 1280) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1"
    )
    graph = page.get_by_role('navigation', name='Graph panes').get_by_role('button', name='Graph', exact=True)
    if graph.is_visible():
        graph.click()


def test_graph_preview_has_one_concise_accessibility_tree_link(page, server_url: str) -> None:
    page.goto(server_url + "/", wait_until="domcontentloaded")
    page.locator('.okf-home-graph > summary').click()
    session = page.context.new_cdp_session(page)
    tree = session.send("Accessibility.getFullAXTree")
    links = [
        node for node in tree["nodes"]
        if node.get("role", {}).get("value") == "link"
        and node.get("name", {}).get("value", "").startswith("Open knowledge graph")
    ]
    assert len(links) == 1
    name = links[0]["name"]["value"]
    assert "concepts" in name and "relationships" in name
    assert not name.startswith("2 2 2"), name


def test_density_policy_covers_small_medium_dense_and_very_dense_thresholds(page, server_url: str) -> None:
    _open(page, server_url)
    policies = page.evaluate(
        "() => [0,24,25,49,50,99,100,249,250,500].map(n => "
        "[n, window.__okfLoomGraph.densityPolicyForCount(n).threshold])"
    )
    assert policies == [
        [0, 0], [24, 0], [25, 25], [49, 25], [50, 50],
        [99, 50], [100, 100], [249, 100], [250, 250], [500, 250],
    ]


def test_graph_load_uses_native_wheel_behavior_without_cytoscape_warning(page, server_url: str) -> None:
    messages: list[str] = []
    page.on("console", lambda message: messages.append(message.text))
    _open(page, server_url)
    assert not any("wheel sensitivity" in message.lower() for message in messages), messages
    canvas = page.locator("#okf-graph")
    box = canvas.bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    start = page.evaluate("() => window.__okfLoomGraph.cy.zoom()")
    # Conventional mouse-wheel profile (Windows/Linux-sized delta).
    page.mouse.wheel(0, -120)
    page.wait_for_timeout(120)
    wheel_zoom = page.evaluate("() => window.__okfLoomGraph.cy.zoom()")
    assert wheel_zoom != start
    # Trackpad-like profile (macOS-sized repeated deltas) remains responsive.
    for _ in range(4):
        page.mouse.wheel(0, 8)
    page.wait_for_timeout(120)
    trackpad_zoom = page.evaluate("() => window.__okfLoomGraph.cy.zoom()")
    assert trackpad_zoom != wheel_zoom


@pytest.mark.parametrize("width", [390, 600, 1280, 1440])
def test_graph_controls_reflow_without_clipping_at_acceptance_widths(page, server_url: str, width: int) -> None:
    _open(page, server_url, width)
    geometry = page.evaluate(
        "() => ({scroll:document.documentElement.scrollWidth, client:document.documentElement.clientWidth, "
        "toolbar:document.querySelector('[aria-label=\"Graph view controls\"]')?.getBoundingClientRect().width || 0})"
    )
    assert geometry["scroll"] <= geometry["client"] + 1, geometry
    assert geometry["toolbar"] > 0, geometry


_TABLET_PANE_JS = """() => {
  const cy = window.__okfLoomGraph && window.__okfLoomGraph.cy;
  const graph = document.getElementById('okf-graph'), g = graph.getBoundingClientRect();
  const layer = graph.querySelector('canvas[data-id="layer2-node"]');
  let painted = 0;
  if (layer && layer.width && layer.height) {
    const px = layer.getContext('2d', {willReadFrequently: true})
      .getImageData(0, 0, layer.width, layer.height).data;
    for (let i = 3; i < px.length; i += 4 * 16) if (px[i] > 0) painted++;
  }
  const zoom = [...document.querySelectorAll('.okf-graph-zoom button')].filter(b => b.offsetParent !== null)
    .map(b => b.getBoundingClientRect());
  return {scroll: document.documentElement.scrollHeight, inner: innerHeight,
          graphTop: g.top, graphBottom: g.bottom, graphHeight: g.height,
          canvasHeight: layer ? layer.clientHeight : 0, painted,
          onScreen: cy ? cy.nodes(':visible').filter(n => { const p = n.renderedPosition();
            return p.x > 0 && p.x < g.width && p.y > 0 && p.y < g.height; }).length : 0,
          zoomBottom: zoom.length ? Math.max(...zoom.map(r => r.bottom)) : null};
}"""


@pytest.mark.parametrize("width", [641, 768, 899])
def test_tablet_graph_pane_is_viewport_bound_and_painted(page, docs_server_url: str, width: int) -> None:
    """T1-002: between the phone (<=640) and desktop bounds the Graph pane
    stayed blank while the auto-height page and the canvas resized each
    other without bound. The page stays one viewport tall, the pane ends at
    the viewport's bottom edge with its zoom controls on screen, and nodes
    are painted inside it."""
    page.set_viewport_size({"width": width, "height": 1024})
    page.add_init_script("try { localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}")
    page.goto(docs_server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
    page.get_by_role('navigation', name='Graph panes').get_by_role('button', name='Graph', exact=True).click()
    page.wait_for_function("() => window.okfGraphCaptureState?.().layoutSettled")
    page.wait_for_timeout(300)
    first = page.evaluate(_TABLET_PANE_JS)
    page.wait_for_timeout(1500)
    later = page.evaluate(_TABLET_PANE_JS)
    for sample in (first, later):
        assert sample["scroll"] == sample["inner"], (first, later)
        assert sample["graphBottom"] <= sample["inner"] + 1, (first, later)
        assert sample["zoomBottom"] is not None and sample["zoomBottom"] <= sample["inner"], (first, later)
    assert later["graphHeight"] == first["graphHeight"] and later["graphHeight"] > 300, (first, later)
    assert abs(later["canvasHeight"] - later["graphHeight"]) <= 1, later
    assert later["painted"] > 0 and later["onScreen"] > 0, later


# Reads the painted node layer: for the largest badge-free round nodes, where
# the type glyph's bounding box is centred, relative to the node centre and
# as a fraction of the node radius. Glyphs are stroked in the palette's
# --graph-node-icon ink on the neutral --graph-node fill, so a glyph pixel is
# one nearer that ink than the fill. Type icons are centred in a 24px viewBox,
# so a correctly inscribed glyph is centred on its node.
_GLYPH_OFFSET_JS = """() => {
  const cy = window.__okfLoomGraph && window.__okfLoomGraph.cy;
  const canvas = cy && cy.container().querySelector('canvas[data-id="layer2-node"]');
  if (!canvas || !canvas.clientWidth) return null;
  const probe = document.createElement('canvas').getContext('2d');
  const rgb = value => { probe.fillStyle = '#000'; probe.fillStyle = value.trim();
    const hex = probe.fillStyle.slice(1); return [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16)); };
  const root = getComputedStyle(document.documentElement);
  const ink = rgb(root.getPropertyValue('--graph-node-icon'));
  const fill = rgb(root.getPropertyValue('--graph-node'));
  const dist = (px, i, c) => Math.hypot(px[i] - c[0], px[i + 1] - c[1], px[i + 2] - c[2]);
  const ratio = canvas.width / canvas.clientWidth;
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  const absent = value => !value || value === 'none';
  const nodes = cy.nodes(':visible').filter(n => n.data('iconUrl') && absent(n.data('commentBadgeUrl'))
    && absent(n.data('brokenBadgeUrl')) && n.style('shape') === 'ellipse' && n.renderedWidth() >= 36);
  const glyphs = nodes.sort((a, b) => b.renderedWidth() - a.renderedWidth()).slice(0, 3).map(n => {
    const p = n.renderedPosition(), radius = n.renderedWidth() / 2 * ratio;
    const cx = p.x * ratio, cy0 = p.y * ratio, scan = radius * 0.85;
    const x0 = Math.floor(cx - scan), y0 = Math.floor(cy0 - scan), size = Math.ceil(scan * 2);
    const px = ctx.getImageData(x0, y0, size, size).data;
    let glyph = 0, left = Infinity, right = -Infinity, top = Infinity, bottom = -Infinity;
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
      const dx = x0 + x + 0.5 - cx, dy = y0 + y + 0.5 - cy0, i = (y * size + x) * 4;
      if (Math.hypot(dx, dy) > scan || px[i + 3] < 200) continue;
      const toInk = dist(px, i, ink);
      if (toInk > 60 || toInk >= dist(px, i, fill)) continue;
      glyph++; left = Math.min(left, dx); right = Math.max(right, dx); top = Math.min(top, dy); bottom = Math.max(bottom, dy);
    }
    const offset = glyph ? Math.hypot((left + right) / 2, (top + bottom) / 2) / radius : null;
    return {id: n.id(), glyph, offset: offset === null ? null : Math.round(offset * 100) / 100};
  });
  return glyphs.length && glyphs.every(g => g.glyph > 0) ? glyphs : null;
}"""


@pytest.mark.parametrize("scale", [1, 2, 3])
def test_node_type_glyphs_stay_inscribed_on_high_density_screens(page, docs_server_url: str, scale: int) -> None:
    """Node type glyphs keep their inscribed size at every devicePixelRatio.

    The glyph is an SVG data URI drawn into the node. Without an intrinsic
    width/height, Chrome drew it about twice too large at devicePixelRatio 2,
    so the node clipped it to its top-left corner (a stray corner mark instead
    of the type icon on every HiDPI screen).
    """
    context = page.context.browser.new_context(
        viewport={"width": 1280, "height": 900}, device_scale_factor=scale)
    try:
        hidpi = context.new_page()
        _open(hidpi, docs_server_url)
        glyphs = hidpi.wait_for_function(_GLYPH_OFFSET_JS, timeout=15_000).json_value()
        assert hidpi.evaluate("() => devicePixelRatio") == scale
        # Measured: ~0.01 when inscribed; ~0.18 for the oversized, clipped glyph.
        assert all(glyph["offset"] <= 0.08 for glyph in glyphs), glyphs
    finally:
        context.close()


@pytest.mark.parametrize("media", ["forced", "reduced"])
def test_graph_composites_keep_non_color_focus_and_reduced_motion_contract(page, server_url: str, media: str) -> None:
    if media == "forced":
        page.emulate_media(forced_colors="active")
    else:
        page.emulate_media(reduced_motion="reduce")
    _open(page, server_url)
    page.locator(".okf-node-index summary").click()
    concepts = page.get_by_role("listbox", name="Graph concepts")
    concepts.focus()
    style = concepts.evaluate(
        "el => {const s=getComputedStyle(el);return {outline:s.outlineStyle,width:parseFloat(s.outlineWidth)}}"
    )
    assert style["outline"] != "none" and style["width"] >= 2
    if media == "reduced":
        assert page.evaluate("() => matchMedia('(prefers-reduced-motion: reduce)').matches")


def test_concepts_and_relationships_are_one_stop_operable_collections(page, server_url: str) -> None:
    _open(page, server_url)
    assert page.get_by_role("link", name="Skip graph visualization").get_attribute("href") == "#okf-concept-index"
    assert page.get_by_role("link", name="Jump to graph results and details").get_attribute("href") == "#okf-detail"
    page.locator(".okf-node-index summary").click()
    concepts = page.get_by_role("listbox", name="Graph concepts")
    expect(concepts).to_have_attribute("tabindex", "0")
    assert concepts.locator('[role="option"][tabindex="-1"]').count() > 1
    assert concepts.locator('button.okf-node-index__item').count() == 0

    page.get_by_text("Graph settings", exact=True).click()
    bundles = page.get_by_role("listbox", name="Bundled relationships")
    if bundles.count():
        expect(bundles).to_have_attribute("tabindex", "0")
        assert bundles.locator("button").count() == 0

    concepts.focus()
    first = concepts.get_attribute("aria-activedescendant")
    page.keyboard.press("End")
    last = concepts.get_attribute("aria-activedescendant")
    assert first != last
    page.keyboard.press("Home")
    page.keyboard.press("Space")
    selected = page.evaluate("() => window.__okfLoomGraph.interaction.snapshot().selected")
    assert selected
    expect(concepts.locator(f'[data-target="{selected}"]')).to_have_attribute("aria-selected", "true")

    relationships = page.get_by_role("listbox", name="Relationships for selected concept")
    if relationships.locator('[role="option"]').count():
        relationships.focus()
        page.keyboard.press("End")
        related = relationships.locator("[role=option]").last.get_attribute("data-related-id")
        page.keyboard.press("Space")
        assert page.evaluate("() => window.__okfLoomGraph.interaction.snapshot().selected") == related


def test_concept_collection_enter_opens_the_active_concept(page, server_url: str) -> None:
    _open(page, server_url)
    page.locator(".okf-node-index summary").click()
    concepts = page.get_by_role("listbox", name="Graph concepts")
    concepts.focus()
    expected = concepts.locator('[role="option"]').first.get_attribute("data-target")
    page.keyboard.press("Enter")
    page.wait_for_url(lambda url: url.endswith(expected))


def test_one_announcer_coalesces_selection_matches_neighborhood_path_and_errors(page, server_url: str) -> None:
    _open(page, server_url, 390)
    announcer = page.locator(".okf-signal__status")
    expect(announcer).to_have_attribute("aria-live", "polite")
    assert page.locator(
        ".okf-detail-selected-status[aria-live], .okf-graph-search-status[aria-live], "
        ".okf-graph-density[aria-live], .okf-mobile-overview__status[aria-live]"
    ).count() == 0

    page.locator('[data-graph-pane="concepts"]').click()
    concepts = page.get_by_role("listbox", name="Graph concepts")
    concepts.focus()
    page.keyboard.press("Space")
    page.wait_for_function("() => document.querySelector('.okf-signal__status').textContent.includes('Selected')")
    selected_message = announcer.text_content()
    assert "Selected" in selected_message and "Map lens" in selected_message

    page.locator('[data-graph-pane="graph"]').click()
    more = page.get_by_role("button", name="More tools")
    if more.is_visible():
        more.click()
    search = page.locator("#okf-search")
    search.fill("no-such-concept")
    page.wait_for_timeout(180)
    expect(announcer).to_contain_text("0 matches")


def test_tour_is_explicit_topmost_and_denial_safe(page, server_url: str) -> None:
    page.add_init_script(
        "Storage.prototype.getItem=()=>{throw new DOMException('denied')};"
        "Storage.prototype.setItem=()=>{throw new DOMException('denied')}"
    )
    _open(page, server_url, 390)
    assert page.get_by_role("dialog", name="Graph tour").count() == 0
    invitation = page.get_by_role("button", name="Take a 45-second graph tour", include_hidden=True)
    reveal_control(page, invitation).click()
    expect(page.get_by_role("dialog", name="Graph tour")).to_be_visible()
    assert page.evaluate(
        "() => window.OKFOverlayStack.top()?.id === 'graph-tour'"
    )
    page.keyboard.press("Escape")
    expect(invitation).to_be_focused()
    assert page.evaluate("() => window.OKFOverlayStack.top()?.id !== 'graph-tour'")


def _coarse_page(page, url: str):
    """A 390px touch context (coarse pointer) sharing the page's browser."""
    context = page.context.browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    coarse = context.new_page()
    coarse.add_init_script("try { localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}")
    coarse.goto(url, wait_until="domcontentloaded")
    coarse.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
    return context, coarse


_SMALL_TARGETS_JS = """selectors => {
  const small = [];
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || el.closest('[hidden]')) continue;
      if (r.height < 44 - 0.5) small.push([selector, (el.textContent || '').trim().slice(0, 30), Math.round(r.height)]);
    }
  }
  return small;
}"""


def test_coarse_pointer_graph_controls_are_44px_targets(page, docs_server_url: str) -> None:
    """D2: at 390 with a coarse pointer the All concepts summary, its rows,
    the ranked starting points and the Graph settings controls are 44px."""
    context, coarse = _coarse_page(page, docs_server_url + "/__graph")
    try:
        coarse.locator('[data-graph-pane="concepts"]').click()
        expect(coarse.locator(".okf-node-index > summary")).to_be_visible()
        small = coarse.evaluate(_SMALL_TARGETS_JS, [".okf-node-index > summary", ".okf-node-index__item"])
        assert small == [], small
        coarse.locator('[data-graph-pane="details"]').click()
        coarse.evaluate("() => { document.querySelector('.okf-signal__advanced').open = true; }")
        small = coarse.evaluate(_SMALL_TARGETS_JS, [
            ".okf-lens-summary__row", ".okf-signal__advanced > summary", ".okf-signal__row--check",
            ".okf-signal select", '.okf-signal input[type="range"]', ".okf-edge-bundle-index summary",
            ".okf-graph-tour-replay"])
        assert small == [], small
    finally:
        context.close()


_PHONE_CHROME_JS = """() => {
  const rect = sel => { const el = document.querySelector(sel); return el && el.getBoundingClientRect(); };
  const tools = rect('.okf-graph-tools'), heading = rect('.okf-graph-tools > h2'), field = rect('#okf-search');
  const presence = rect('.okf-mobile-presence'), presenceButton = rect('.okf-mobile-presence button');
  const dock = [...document.querySelectorAll('.okf-graph-viewport-toolbar button')].map(b => b.getBoundingClientRect());
  const studio = rect('.okf-shell__studio-slot'), appearance = rect('.okf-shell__appearance'), tabs = rect('.okf-graph-mobile-nav');
  return {inner: innerHeight, layoutTop: rect('.okf-layout').top, graphTop: rect('#okf-graph').top,
          toolsHeight: tools.height, headingMid: heading.top + heading.height / 2, fieldMid: field.top + field.height / 2,
          presenceHeight: presence.height, presenceButtonHeight: presenceButton.height,
          presenceLabels: [...document.querySelectorAll('.okf-mobile-presence button, [data-graph-pane]')]
            .map(b => b.textContent.trim()),
          dockRows: new Set(dock.map(r => Math.round(r.top))).size,
          studioTop: studio && studio.top, appearanceTop: appearance.top, tabsTop: tabs.top};
}"""


@pytest.mark.parametrize("size", [(390, 844), (320, 568)])
def test_phone_graph_chrome_leaves_the_viewport_to_the_graph(page, docs_server_url: str, size) -> None:
    """M1-002/M1-003/M1-004: the phone graph route's own rows stay compact:
    the presence status is one row whose button is not a second "Details",
    Find in graph is one row, the viewport actions are one dock row from
    375px, Appearance stays with the shell above the graph rows at 320, and
    the graph is on screen at load."""
    width, height = size
    context = page.context.browser.new_context(
        viewport={"width": width, "height": height}, has_touch=True, is_mobile=True)
    try:
        phone = context.new_page()
        phone.add_init_script("try { localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}")
        phone.goto(docs_server_url + "/__graph", wait_until="domcontentloaded")
        phone.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
        phone.locator('[data-graph-pane="graph"]').click()
        expect(phone.locator(".okf-mobile-presence__agent")).not_to_be_empty()
        geo = phone.evaluate(_PHONE_CHROME_JS)
        assert geo["presenceHeight"] <= geo["presenceButtonHeight"] + 10, geo
        assert geo["presenceLabels"].count("Details") == 1, geo
        assert abs(geo["headingMid"] - geo["fieldMid"]) <= 2 and geo["toolsHeight"] <= 70, geo
        assert geo["graphTop"] <= geo["inner"] - 160, geo
        if width >= 375:
            assert geo["dockRows"] == 1, geo
            assert geo["layoutTop"] <= 0.45 * geo["inner"], geo
        else:
            assert geo["appearanceTop"] == geo["studioTop"] and geo["appearanceTop"] < geo["tabsTop"], geo
        hidden_label = phone.locator(".okf-graph-tools__search > span").first
        assert hidden_label.evaluate("el => el.getBoundingClientRect().width") <= 1
        phone.get_by_role("searchbox", name="Search graph concepts").fill("no-such-concept")
        expect(phone.locator(".okf-graph-search-status")).not_to_be_empty()
        assert phone.locator(".okf-graph-search-status").evaluate("el => el.getBoundingClientRect().width") > 1
    finally:
        context.close()


def test_graph_settings_flow_in_the_pane_and_chip_rings_are_whole(page, docs_server_url: str) -> None:
    """D2-002/D2-012: Graph settings have no nested scroller inside the
    detail pane, and a keyboard-focused joined filter chip paints its whole
    ring: no ancestor below the pane clips it and the joined Only button
    does not paint over it."""
    _open(page, docs_server_url, 1440)
    page.locator(".okf-signal__advanced > summary").click()
    chip = page.locator(".okf-filter-chip-group > .okf-filter-chip").first
    chip.scroll_into_view_if_needed()
    chip.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    seen = page.evaluate("""() => {
      const body = document.querySelector('.okf-signal__body'), el = document.activeElement;
      const clips = [];
      for (let a = el.parentElement; a && !a.classList.contains('okf-detail'); a = a.parentElement) {
        const cs = getComputedStyle(a);
        if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') clips.push(a.className);
      }
      const solo = el.nextElementSibling, cs = getComputedStyle(el);
      return {scroll: body.scrollHeight, client: body.clientHeight, focusVisible: el.matches(':focus-visible'),
              isChip: el.classList.contains('okf-filter-chip'), clips, outline: cs.outlineStyle,
              above: cs.position !== 'static' && Number(cs.zIndex) > (Number(getComputedStyle(solo).zIndex) || 0)};
    }""")
    assert seen["scroll"] == seen["client"], seen
    assert seen["isChip"] and seen["focusVisible"] and seen["outline"] == "solid", seen
    assert seen["clips"] == [] and seen["above"], seen


def test_selected_concept_body_keeps_ledger_prose_and_contained_code(page, docs_server_url: str) -> None:
    """D3-002/D2-003: the details pane renders a selected concept with the
    document page's prose styles: padded code wells whose long lines scroll
    inside their own scrollport, inline-code chips and underlined links."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(docs_server_url + "/__graph?selected=reference/cli", wait_until="domcontentloaded")
    page.wait_for_function("() => document.querySelector('#detail-body pre')")
    seen = page.evaluate("""() => {
      const body = document.getElementById('detail-body'), pane = document.getElementById('okf-detail');
      const wells = [...body.querySelectorAll('pre')].map(pre => {
        let port = pre.parentElement;
        while (port !== body && getComputedStyle(port).overflowX === 'visible') port = port.parentElement;
        const cs = getComputedStyle(pre);
        return {pad: parseFloat(cs.paddingLeft), fill: cs.backgroundColor !== 'rgba(0, 0, 0, 0)',
                contained: port !== body && port.getBoundingClientRect().right <= pane.getBoundingClientRect().right + 0.5,
                scrolls: pre.scrollWidth <= port.clientWidth || port.scrollWidth > port.clientWidth};
      });
      const code = body.querySelector('p code'), link = body.querySelector('p a');
      return {prose: body.classList.contains('okf-prose'), wells,
              chip: code && getComputedStyle(code).backgroundColor !== 'rgba(0, 0, 0, 0)',
              underline: link && getComputedStyle(link).textDecorationLine};
    }""")
    assert seen["prose"] and seen["wells"], seen
    assert all(w["pad"] >= 12 and w["fill"] and w["contained"] and w["scrolls"] for w in seen["wells"]), seen
    assert seen["chip"] and seen["underline"] == "underline", seen


@pytest.mark.parametrize("height", [900, 800])
def test_open_concept_list_keeps_rows_steady_and_above_zoom(page, docs_server_url: str, height: int) -> None:
    """D2-004/D2-005: moving the active option keeps every row one line
    (the 2px bar marks it, no squeezed "Active ·" column), and the open
    list is hit-tested above the zoom cluster that overlaps its lower end."""
    page.set_viewport_size({"width": 1440 if height == 900 else 1280, "height": height})
    page.goto(docs_server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
    page.locator(".okf-node-index summary").click()
    concepts = page.get_by_role("listbox", name="Graph concepts")
    concepts.focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    seen = page.evaluate("""() => {
      const list = document.querySelector('.okf-node-index__list');
      const items = [...list.querySelectorAll('[role="option"]')];
      const active = list.querySelector('.is-active'), plain = items.find(i => i !== active);
      const lr = list.getBoundingClientRect();
      const hits = [...document.querySelectorAll('#okf-graph > .okf-graph-zoom button')].map(b => b.getBoundingClientRect())
        .filter(b => b.left < lr.right && lr.left < b.right && b.top < lr.bottom && lr.top < b.bottom)
        .map(b => { const el = document.elementFromPoint((Math.max(b.left, lr.left) + Math.min(b.right, lr.right)) / 2,
                                                         (Math.max(b.top, lr.top) + Math.min(b.bottom, lr.bottom)) / 2);
                    return !!(el && el.closest('.okf-node-index')); });
      return {activeH: active.getBoundingClientRect().height, plainH: plain.getBoundingClientRect().height,
              before: getComputedStyle(active, '::before').content, bar: getComputedStyle(active).borderInlineStartWidth, hits};
    }""")
    assert seen["activeH"] == seen["plainH"] and seen["before"] in ("none", "normal"), seen
    assert seen["bar"] == "2px", seen
    assert all(seen["hits"]), seen


def test_tablet_touch_graph_settings_controls_are_44px_targets(page, docs_server_url: str) -> None:
    """T1-006 (graph): on a coarse-pointer tablet with the desktop graph
    layout, legend items and the filter chip, Only and Show all buttons
    keep the 44px touch floor."""
    context = page.context.browser.new_context(
        viewport={"width": 1024, "height": 1366}, has_touch=True, is_mobile=True)
    try:
        tablet = context.new_page()
        tablet.add_init_script("try { localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}")
        tablet.goto(docs_server_url + "/__graph", wait_until="domcontentloaded")
        tablet.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
        tablet.evaluate("() => { document.querySelector('.okf-signal__advanced').open = true; }")
        expect(tablet.locator(".okf-filter-solo").first).to_be_visible()
        small = tablet.evaluate(_SMALL_TARGETS_JS, [
            "button.okf-graph-legend__item", ".okf-filter-chip", ".okf-filter-solo", ".okf-filter-showall"])
        assert small == [], small
    finally:
        context.close()


def test_graph_settings_controls_are_ledger_controls(page, docs_server_url: str) -> None:
    """D2-009/D2-011/D2-023: each type chip shares its row with its Only
    button; Re-run layout is a labelled Ledger button; sliders and
    checkboxes are square token-drawn controls, not accent-filled natives."""
    _open(page, docs_server_url, 1440)
    page.locator(".okf-signal__advanced > summary").click()
    seen = page.evaluate("""() => {
      const types = document.querySelector('.okf-filter-chips[aria-label="Type filters"]');
      const pairs = [...types.querySelectorAll(':scope > .okf-filter-chip')].map(chip => {
        const solo = chip.nextElementSibling, a = chip.getBoundingClientRect(), b = solo.getBoundingClientRect();
        return solo.classList.contains('okf-filter-solo') && Math.abs(a.top - b.top) < 1 && Math.abs(a.right - b.left) < 1.5;
      });
      const rerun = document.querySelector('.okf-graph-layout-settings > button');
      const label = rerun.querySelector('.okf-viewport-label');
      const look = sel => { const el = document.querySelector(sel), cs = getComputedStyle(el);
        return {appearance: cs.appearance, radius: cs.borderTopLeftRadius, accent: cs.accentColor}; };
      return {pairs, rerun: {btn: rerun.classList.contains('okf-btn'), label: label.textContent,
                             shown: label.getBoundingClientRect().width > 0, width: rerun.getBoundingClientRect().width,
                             pane: rerun.parentElement.getBoundingClientRect().width},
              range: look('.okf-signal input[type="range"]'), check: look('.okf-signal input[type="checkbox"]')};
    }""")
    assert seen["pairs"] and all(seen["pairs"]), seen
    assert seen["rerun"]["btn"] and seen["rerun"]["label"] == "Re-run layout" and seen["rerun"]["shown"], seen
    assert seen["rerun"]["width"] < seen["rerun"]["pane"] / 2, seen
    for control in (seen["range"], seen["check"]):
        assert control["appearance"] == "none" and control["radius"] == "0px", seen


def test_details_pane_reads_as_one_consistent_column(page, docs_server_url: str) -> None:
    """D2-022/T1-008/T1-012: one heading per section, single-line count
    figures in a narrow pane, one left rail for pane content at tablet
    widths, and a broken-link marker that says what it marks."""
    _open(page, docs_server_url, 901)
    narrow = page.evaluate("""() => {
      const notes = [...document.querySelectorAll('#detail-empty .okf-lens-summary__note')];
      const digest = document.querySelector('.okf-graph-theme-digest');
      return {wrapped: notes.filter(n => n.getClientRects().length !== 1 ||
                n.getBoundingClientRect().height > parseFloat(getComputedStyle(n).lineHeight || '16') * 1.5).length,
              notes: notes.length,
              digestHeadings: digest ? digest.querySelectorAll('h2').length : null,
              verdict: digest ? digest.querySelector('.okf-lens-summary__verdict').textContent : ''};
    }""")
    assert narrow["notes"] and narrow["wrapped"] == 0, narrow
    assert narrow["digestHeadings"] == 0 and " - " not in narrow["verdict"], narrow
    page.set_viewport_size({"width": 768, "height": 1024})
    page.get_by_role('navigation', name='Graph panes').get_by_role('button', name='Details', exact=True).click()
    rails = page.evaluate("""() => ['.okf-detail__empty > p', '#detail-empty .okf-lens-summary__title',
        '.okf-signal__lenses > summary', '.okf-signal__advanced > summary', '.okf-signal__question']
      .map(s => Math.round(document.querySelector(s).getBoundingClientRect().left))""")
    assert len(set(rails)) == 1, rails
    page.get_by_role('navigation', name='Graph panes').get_by_role('button', name='Concepts', exact=True).click()
    warn = page.locator(".okf-node-index__warn").first
    expect(warn).to_have_text(re.compile(r"^\d+ broken$"))
    expect(warn).to_have_attribute("title", re.compile(r"^\d+ broken links?$"))


def test_graph_settings_disclosure_matches_its_sibling_summaries(page, docs_server_url: str) -> None:
    """Graph settings opens like Explore with lenses: the native marker on
    the left in body type, not a hidden marker with a trailing glyph; the
    tour replay is a Ledger button in the body face."""
    _open(page, docs_server_url, 1440)
    seen = page.evaluate("""() => {
      const look = el => { const cs = getComputedStyle(el);
        return {display: cs.display, list: cs.listStyleType, family: cs.fontFamily, after: getComputedStyle(el, '::after').content}; };
      const replay = getComputedStyle(document.querySelector('.okf-graph-tour-replay'));
      return {settings: look(document.querySelector('.okf-signal__advanced > summary')),
              lenses: look(document.querySelector('.okf-signal__lenses > summary')),
              replay: {family: replay.fontFamily, border: replay.borderTopStyle}};
    }""")
    assert seen["settings"] == seen["lenses"], seen
    assert seen["settings"]["list"] == "disclosure-closed", seen
    assert "Public Sans" in seen["replay"]["family"] and seen["replay"]["border"] != "outset", seen


def test_graph_390_title_and_tools_share_the_pane_gutter(page, docs_server_url: str) -> None:
    """The route title and Find in graph sit on the 12px gutter of the pane
    tabs, not an 8px one."""
    _open(page, docs_server_url, 390)
    edges = page.evaluate("""() => {
      const left = sel => { const el = document.querySelector(sel);
        const cs = getComputedStyle(el); return el.getBoundingClientRect().left + parseFloat(cs.paddingLeft); };
      return {title: left('.okf-graph-route-title'), tools: left('.okf-graph-tools'),
              tabs: document.querySelector('.okf-graph-mobile-nav').getBoundingClientRect().left};
    }""")
    # The pane-tab row's box edge is the shell gutter at this width.
    assert edges["title"] == edges["tools"] == edges["tabs"] == 12, edges


def test_single_file_short_canvas_keeps_zoom_clear_of_concept_index(tmp_path, page) -> None:
    """With the Search & context drawer open the single-file canvas is short:
    the zoom controls become a bottom-left row and never cover All concepts;
    closing the drawer restores the right-hand column."""
    from okf_loom import Bundle
    from okf_loom.render import render_single_file
    from test_viewer_browser import DOCS_BUNDLE

    out = tmp_path / "single.html"
    render_single_file(Bundle.load(DOCS_BUNDLE), out)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.add_init_script("try { localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}")
    page.goto(out.as_uri() + "#okf-graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfGraphCaptureState && window.okfGraphCaptureState().layoutSettled")
    page.wait_for_function("() => !document.getElementById('okf-single-search-toggle').disabled")
    geometry_js = """() => {
      const graph = document.getElementById('okf-graph');
      const g = graph.getBoundingClientRect();
      const s = document.querySelector('#okf-graph > .okf-node-index > summary').getBoundingClientRect();
      const buttons = [...document.querySelectorAll('#okf-graph > .okf-graph-zoom button')]
        .filter(b => b.offsetParent !== null).map(b => b.getBoundingClientRect());
      const overlap = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
      return {short: graph.hasAttribute('data-okf-graph-short'), height: g.height,
              covers: buttons.some(b => overlap(b, s)),
              rightEdge: Math.max(...buttons.map(b => b.right)) > g.right - 60};
    }"""
    wide = page.evaluate(geometry_js)
    assert not wide["short"] and not wide["covers"] and wide["rightEdge"], wide

    page.locator("#okf-single-search-toggle").click()
    # Results grow the drawer to its cap and leave a short canvas.
    page.fill("#okf-single-query", "comment")
    page.locator("#okf-single-search form button[type='submit']").click()
    page.wait_for_function("() => document.querySelectorAll('[data-single-results] > li').length > 3")
    page.wait_for_function("() => document.getElementById('okf-graph').hasAttribute('data-okf-graph-short')")
    short = page.evaluate(geometry_js)
    assert short["short"] and not short["covers"], short

    page.locator("[data-single-search-close]").click()
    page.wait_for_function("() => !document.getElementById('okf-graph').hasAttribute('data-okf-graph-short')")
    restored = page.evaluate(geometry_js)
    assert not restored["covers"] and restored["rightEdge"], restored
