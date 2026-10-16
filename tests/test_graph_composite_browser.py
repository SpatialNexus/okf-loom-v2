"""Browser proof for graph composite widgets and synchronized interaction state."""
from __future__ import annotations

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


# Ink extent (height, width) of each labelled node's painted label on the node
# layer, keyed by node id. Label ink is the dark --graph-label; edges and
# halos are lighter, so a dark-pixel box is the rasterised text itself.
_LABEL_INK_JS = """(ids) => {
  const cy = window.__okfLoomGraph.cy;
  const canvas = cy.container().querySelector('canvas[data-id="layer2-node"]');
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  const ratio = canvas.width / canvas.clientWidth, out = {};
  const nodes = ids ? cy.collection(ids.map(id => cy.getElementById(id)))
    : cy.nodes(':visible').filter(n => n.style('label') && String(n.data('label') || '').length > 6);
  nodes.forEach(n => {
    const bb = n.renderedBoundingBox({includeNodes: false, includeEdges: false, includeLabels: true});
    const x0 = Math.round(bb.x1 * ratio), y0 = Math.round(bb.y1 * ratio);
    const w = Math.round(bb.w * ratio), h = Math.round(bb.h * ratio);
    if (w < 4 || h < 4 || x0 < 0 || y0 < 0 || x0 + w > canvas.width || y0 + h > canvas.height) return;
    const px = ctx.getImageData(x0, y0, w, h).data;
    let top = h, bottom = -1, left = w, right = -1;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      if (px[i + 3] > 200 && px[i] + px[i + 1] + px[i + 2] < 330) {
        top = Math.min(top, y); bottom = Math.max(bottom, y); left = Math.min(left, x); right = Math.max(right, x);
      }
    }
    if (bottom >= 0) out[n.id()] = [bottom - top + 1, right - left + 1];
  });
  return out;
}"""


def test_canvas_labels_rerasterise_after_window_resize(page, docs_server_url: str) -> None:
    """D2-017: crossing the 900px readable-label switch and back leaves every
    node label painted exactly like a fresh load at the same size.

    Cytoscape rasterised the first label texture under the new label style
    against the label box cached for the old one, so after 1440 -> 900 ->
    1440 -> 1280 the 13px labels were squashed into the 25px plate (and
    stretched the other way at 900) until the next restyle.
    """
    _open(page, docs_server_url, 1280)
    page.wait_for_timeout(1200)
    fresh_zoom = page.evaluate("() => window.__okfLoomGraph.cy.zoom()")
    fresh = page.evaluate(_LABEL_INK_JS, None)
    assert len(fresh) >= 4, fresh
    for width, height, size in [(1440, 900, "13px"), (900, 567, "25px"), (1440, 900, "13px"), (1280, 900, "13px")]:
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_function(
            "size => window.__okfLoomGraph.cy.nodes().first().style('font-size') === size", arg=size)
        page.wait_for_timeout(600)
    page.wait_for_function("z => Math.abs(window.__okfLoomGraph.cy.zoom() - z) < 1e-6", arg=fresh_zoom)
    page.wait_for_timeout(400)
    resized = page.evaluate(_LABEL_INK_JS, list(fresh))
    assert set(resized) == set(fresh), (fresh, resized)
    # Sub-pixel placement moves an edge by a pixel at most; a stale texture
    # squashed the 13px ink to about 0.7 of its height.
    distorted = {nid: (fresh[nid], resized[nid]) for nid in fresh
                 if abs(resized[nid][0] - fresh[nid][0]) > 1 or abs(resized[nid][1] - fresh[nid][1]) > 3}
    assert not distorted, distorted


# Painted label overprints: pairs of visible node labels (inked text box) and
# bundled-relationship count chips (whole chip) that overlap on the canvas.
_LABEL_OVERPRINT_JS = """() => {
  const cy = window.__okfLoomGraph.cy;
  const O = {includeNodes: false, includeEdges: false, includeLabels: true, includeOverlays: false};
  const shown = e => e.visible() && !!e.style('label') && Number(e.style('text-opacity')) > 0
    && Number(e.style('opacity')) > 0.5;
  const box = (e, inset) => { const b = e.boundingBox(O);
    return {id: e.isNode() ? e.data('label') : 'chip ' + e.id(), x1: b.x1 + inset, y1: b.y1 + inset, x2: b.x2 - inset, y2: b.y2 - inset}; };
  const boxes = cy.nodes().filter(shown).map(n => box(n, 6))
    .concat(cy.edges('.okf-multi-edge').filter(shown).map(e => box(e, 1)));
  const hits = [];
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    if (a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2) hits.push([a.id, b.id]);
  }
  return hits;
}"""


def test_selected_label_takes_precedence_and_overprints_are_culled(page, docs_server_url: str) -> None:
    """D2-006: the selected concept's name sits above its own incident edges
    on an opaque --surface plate and shows its full title; lower-priority
    labels and count chips that would overprint another label are hidden,
    at fit zoom and zoomed in."""
    _open(page, docs_server_url, 1440)
    page.locator('.okf-lens-summary__row[data-target="reference/cli"]').first.click()
    page.wait_for_function("() => window.__okfLoomGraph.cy.$(':selected').id() === 'reference/cli'")
    page.wait_for_timeout(600)
    selected = page.evaluate("""() => {
      const cy = window.__okfLoomGraph.cy, n = cy.$(':selected');
      const probe = document.createElement('canvas').getContext('2d');
      const norm = c => { probe.fillStyle = '#000'; probe.fillStyle = c; return probe.fillStyle; };
      const surface = norm(getComputedStyle(document.documentElement).getPropertyValue('--surface').trim());
      return {plate: norm(n.style('text-background-color')), surface, opacity: Number(n.style('text-background-opacity')),
              depth: n.style('z-compound-depth'), width: parseFloat(n.style('text-max-width')),
              culled: n.hasClass('okf-label-culled'), incident: n.connectedEdges('.okf-incident').length,
              incidentDepth: n.connectedEdges('.okf-incident').first().style('z-compound-depth')};
    }""")
    assert selected["plate"] == selected["surface"] and selected["opacity"] == 1, selected
    # The name draws above its fan of incident edges, which stay below nodes.
    assert selected["depth"] == "top" and selected["incident"] > 10 and selected["incidentDepth"] != "top", selected
    assert selected["width"] >= 200 and not selected["culled"], selected
    assert page.evaluate(_LABEL_OVERPRINT_JS) == []
    for _ in range(2):
        page.get_by_role("button", name="Zoom in").click()
        page.wait_for_timeout(400)
    page.wait_for_timeout(400)
    assert page.evaluate(_LABEL_OVERPRINT_JS) == []
    assert not page.evaluate("() => window.__okfLoomGraph.cy.$(':selected').hasClass('okf-label-culled')")


_FIND_BAND_JS = """() => {
  const rect = s => { const e = document.querySelector(s); const b = e.getBoundingClientRect();
    return {x: b.left, y: b.top, w: b.width, h: b.height, r: b.right, b: b.bottom, visible: b.width > 0 && getComputedStyle(e).visibility !== 'hidden'}; };
  const input = document.querySelector('#okf-search');
  const status = document.querySelector('.okf-graph-search-status');
  return {band: rect('.okf-graph-tools'), heading: rect('.okf-graph-tools > h2'), input: rect('#okf-search'),
          clear: rect('.okf-graph-search-clear'), title: rect('.okf-graph-route-title'), canvas: rect('#okf-graph'),
          status: status.textContent, statusCase: getComputedStyle(status).textTransform,
          clearButtons: [...document.querySelectorAll('.okf-graph-tools button')].filter(b => b.offsetParent && /clear/i.test(b.textContent)).length};
}"""


@pytest.mark.parametrize("width", [900, 1440])
def test_find_in_graph_band_stays_put_while_typing(page, docs_server_url: str, width: int) -> None:
    """D2-007: typing in Find in graph never moves the band, heading or
    canvas; there is one clear control; the status quotes the query as typed."""
    _open(page, docs_server_url, width)
    search = page.get_by_role("searchbox", name="Search graph concepts")
    idle = page.evaluate(_FIND_BAND_JS)
    # The heading shares the field's centre line on the route's gutter.
    assert abs((idle["heading"]["y"] + idle["heading"]["h"] / 2) - (idle["input"]["y"] + idle["input"]["h"] / 2)) <= 1, idle
    assert idle["heading"]["x"] == idle["band"]["x"] + (20 if width > 900 else 12), idle
    for query in ["spec", "zzqqXX", "a deliberately long query that runs well past the end of the field"]:
        search.fill(query)
        page.wait_for_function("q => document.querySelector('.okf-graph-search-status').textContent !== '' && window.__okfLoomGraph.getState", arg=query)
        page.wait_for_timeout(250)
        typed = page.evaluate(_FIND_BAND_JS)
        for part in ("band", "heading", "input", "title", "canvas"):
            assert typed[part] == idle[part], (query, part, idle[part], typed[part])
        assert typed["clearButtons"] == 1, typed
        clear, field = typed["clear"], typed["input"]
        assert field["x"] < clear["x"] and clear["r"] <= field["r"] and clear["y"] >= field["y"] and clear["b"] <= field["b"], typed
        assert typed["statusCase"] == "none", typed
        if query == "zzqqXX":
            assert typed["status"] == "No concepts match \u201czzqqXX\u201d", typed
    # Chrome paints its own cancel glyph in a hovered search field with a
    # value; the field's end must look the same with that glyph suppressed.
    search.fill("spec")
    search.hover()
    page.wait_for_timeout(200)
    field = page.evaluate(_FIND_BAND_JS)["input"]
    end = {"x": field["x"] + field["w"] / 2, "y": field["y"] + 1, "width": field["w"] / 2 - 1, "height": field["h"] - 2}
    painted = page.screenshot(clip=end)
    page.add_style_tag(content="#okf-search::-webkit-search-cancel-button{display:none!important}")
    page.wait_for_timeout(100)
    assert page.screenshot(clip=end) == painted, "native search cancel button still paints beside Clear"
    page.get_by_role("button", name="Clear", exact=True).click()
    expect(search).to_have_value("")
    expect(search).to_be_focused()
    assert page.evaluate(_FIND_BAND_JS)["band"] == idle["band"]


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
