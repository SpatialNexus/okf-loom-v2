"""Browser proof for graph composite widgets and synchronized interaction state."""
from __future__ import annotations

import pytest

from browser_support import expect, reveal_control

from test_viewer_browser import page, server_url  # noqa: E402

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
