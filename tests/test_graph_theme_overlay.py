"""Focused current-v2 graph consumer proofs.

These browser checks deliberately exercise the real graph page instead of
asserting only source strings: graph.js consumes theme.js events, paints
Cytoscape/minimap state, and the mobile graph chrome shares the page with the
non-modal Appearance overlay.
"""
from __future__ import annotations

import pytest

from browser_support import expect

from test_viewer_browser import (  # noqa: E402
    _open_graph,
    docs_server_url,
    page,
    server_url,
)

pytestmark = pytest.mark.browser


def _open_mobile_graph(page, server_url: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script(
        "try { localStorage.setItem('okfGraphTourDone', '1'); "
        "['okf-theme-family','okf-theme-mode','okf-theme-contrast','okf-theme-border','okf-theme']"
        ".forEach(k => localStorage.removeItem(k)); } catch (e) {}"
    )
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1"
    )


@pytest.mark.parametrize("width", [320, 390])
def test_mobile_graph_appearance_overlay_has_topmost_pointer_and_keyboard_controls(
    server_url: str, page, width: int
) -> None:
    """The mobile graph chrome stays below the fixed, non-modal theme menu."""
    _open_mobile_graph(page, server_url, width)

    expect(page.get_by_role("searchbox", name="Search graph concepts")).to_be_visible()
    trigger = page.locator("#okf-theme")
    expect(trigger).to_be_visible()

    # Keyboard open + Arrow/Home/End remains owned by theme.js.
    trigger.focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => !document.getElementById('okf-appearance-menu').hidden"
    )
    assert page.evaluate(
        "() => document.activeElement?.closest('#okf-appearance-menu') !== null"
    )
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "dark"
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "auto"

    geometry = page.evaluate(
        """() => {
          const menu = document.getElementById('okf-appearance-menu');
          const option = menu.querySelector(
            'input[data-okf-set="mode"][data-okf-val="auto"]'
          );
          const r = menu.getBoundingClientRect();
          const o = option.getBoundingClientRect();
          const hit = document.elementFromPoint(o.left + o.width / 2, o.top + o.height / 2);
          const z = el => Number.parseInt(getComputedStyle(el).zIndex, 10) || 0;
          const nav = document.querySelector('.okf-graph-mobile-nav');
          const zoom = document.querySelector('.okf-graph-zoom');
          const actions = document.querySelector('.okf-graph-actions');
          return {
            menu: {left: r.left, right: r.right, top: r.top, bottom: r.bottom,
              width: r.width, height: r.height, z: z(menu)},
            hitInsideMenu: menu.contains(hit),
            chromeZ: [nav, zoom, actions].filter(Boolean).map(z),
            scrollWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
          };
        }"""
    )
    assert geometry["menu"]["left"] >= -0.5
    assert geometry["menu"]["right"] <= width + 0.5
    assert geometry["menu"]["top"] >= -0.5
    assert geometry["menu"]["bottom"] <= 844 + 0.5
    assert geometry["hitInsideMenu"], geometry
    assert all(z < geometry["menu"]["z"] for z in geometry["chromeZ"]), geometry
    assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry

    # Escape remains the menu's topmost-layer action and restores its trigger.
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    assert page.locator("#okf-theme").evaluate("el => document.activeElement === el")

    # Pointer activation reaches an option rather than graph chrome.
    trigger.click()
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    page.locator(
        '#okf-appearance-menu input[data-okf-set="mode"][data-okf-val="dark"]'
    ).click()
    expect(page.locator("html")).to_have_attribute("data-theme", "technical-dark")


def _graph_theme_snapshot(page):
    return page.evaluate(
        """() => {
          const G = window.__okfLoomGraph;
          const cy = G.cy;
          const css = value => {
            const el = document.createElement('span');
            el.style.color = value;
            return el.style.color.replace(/\\s+/g, '');
          };
          const first = selector => cy.elements(selector).first();
          const node = first('node.okf-path');
          const baseNode = cy.nodes().first();
          const hover = first('node.okf-hover');
          const hub = first('node.okf-hub');
          const selected = first('node:selected');
          const path = first('edge.okf-path');
          const multi = first('edge.okf-multi-edge');
          const canvas = document.querySelector('.okf-graph-minimap canvas');
          const pixels = canvas ? canvas.getContext('2d').getImageData(
            0, 0, canvas.width, canvas.height
          ).data : [];
          const colours = {};
          for (let i = 0; i < pixels.length; i += 4) {
            if (!pixels[i + 3]) continue;
            const key = `${pixels[i]},${pixels[i + 1]},${pixels[i + 2]}`;
            colours[key] = (colours[key] || 0) + 1;
          }
          const style = (el, names) => Object.fromEntries(
            names.map(name => [name, el && el.length ? css(el.style(name)) : null])
          );
          return {
            theme: document.documentElement.dataset.theme,
            baseNode: style(baseNode, ['color', 'text-background-color', 'border-color']),
            pathNode: style(node, ['underlay-color', 'border-color']),
            hover: style(hover, ['underlay-color']),
            hub: style(hub, ['underlay-color']),
            selected: style(selected, ['border-color']),
            pathEdge: style(path, ['line-color', 'target-arrow-color']),
            multi: style(multi, ['text-border-color']),
            minimapColours: colours,
            capture: window.okfGraphCaptureState(),
            layout: G.layoutStats.lastAppliedSeq,
            hasTypedPath: !!(path && path.length && path.hasClass('okf-rel-edge')),
          };
        }"""
    )


def test_graph_theme_event_repaints_every_stateful_cue_and_minimap(
    docs_server_url: str, page
) -> None:
    """Resolved theme events repaint path, hover/hub, selection, bundles, and overview."""
    _open_graph(page, docs_server_url)
    page.evaluate("() => window.OKFLoomTheme.setTheme('technical-light')")
    page.wait_for_function("() => document.documentElement.dataset.theme === 'technical-light'")

    page.locator('button[aria-label="Show minimap"]').click()
    page.wait_for_function("() => !document.querySelector('.okf-graph-minimap').hidden")
    setup = page.evaluate(
        """() => {
          const G = window.__okfLoomGraph;
          const cy = G.cy;
          const typed = cy.edges().filter(e => !!e.data('label')).first();
          const start = typed.source().id();
          const end = typed.target().id();
          G.interaction.dispatch({type: 'SELECT', id: start, source: 'test', quietUrl: true});
          const traced = G.tracePath(cy.getElementById(start), cy.getElementById(end));
          const candidate = cy.nodes().filter(n =>
            !n.hasClass('okf-path') && n.id() !== start
          ).first();
          G.interaction.dispatch({
            type: 'HOVER', id: candidate.id(), position: candidate.renderedPosition(), quietUrl: true
          });
          return {
            traced,
            typedPathEdge: typed.id(),
            pathCount: cy.elements('.okf-path').length,
            multiCount: cy.edges('.okf-multi-edge').length,
            hubCount: cy.nodes('.okf-hub').length,
          };
        }"""
    )
    assert setup["traced"] is True, setup
    assert setup["pathCount"] >= 2, setup
    assert setup["multiCount"] > 0, setup
    assert setup["hubCount"] > 0, setup
    page.wait_for_function("() => window.__okfLoomGraph.cy.nodes('.okf-hover').length === 1")
    before = _graph_theme_snapshot(page)
    assert before["hasTypedPath"], before

    page.evaluate("() => window.OKFLoomTheme.setTheme('technical-dark')")
    page.wait_for_function("() => document.documentElement.dataset.theme === 'technical-dark'")
    page.wait_for_function("""() => {
      const state = window.okfGraphCaptureState && window.okfGraphCaptureState();
      return state && state.layoutSettled && !state.transitionPending;
    }""")
    after = _graph_theme_snapshot(page)

    expected_select = "rgb(62,201,201)"
    assert after["pathNode"]["underlay-color"] == expected_select, after
    assert after["pathNode"]["border-color"] == expected_select, after
    assert after["hover"]["underlay-color"] == expected_select, after
    assert after["hub"]["underlay-color"] == expected_select, after
    assert after["selected"]["border-color"] == expected_select, after
    assert after["pathEdge"]["line-color"] == expected_select, after
    assert after["pathEdge"]["target-arrow-color"] == expected_select, after
    assert after["multi"]["text-border-color"] == expected_select, after
    assert after["baseNode"]["color"] == "rgb(226,232,240)", after
    assert after["baseNode"]["text-background-color"] == "rgb(11,18,32)", after
    assert after["minimapColours"] != before["minimapColours"], (
        "minimap pixels did not change with the resolved theme event",
        before["minimapColours"], after["minimapColours"],
    )
    assert after["pathNode"]["underlay-color"] != before["pathNode"]["underlay-color"]
    assert after["capture"]["layoutSettled"] is True, after
    assert after["layout"] == before["layout"], (before, after)
    assert after["capture"]["appliedLayoutGeneration"] == before["capture"]["appliedLayoutGeneration"]
    assert after["capture"]["suspended"] is False
    assert after["capture"].get("terminalCode") is None
    assert after["capture"]["initialized"] is True
    assert after["capture"]["nodeCount"] == before["capture"]["nodeCount"]


@pytest.mark.parametrize(
    ("theme", "node_text"),
    [
        ("purple-light", "rgb(36,27,54)"),
        ("purple-dark", "rgb(241,234,250)"),
        ("coffee-light", "rgb(51,36,27)"),
        ("coffee-dark", "rgb(244,231,215)"),
    ],
)
def test_optional_family_graph_palettes_repaint_canvas_and_minimap(
    docs_server_url: str, page, theme: str, node_text: str
) -> None:
    """Every optional resolved theme reaches Cytoscape and the overview map."""
    _open_graph(page, docs_server_url)
    page.locator('button[aria-label="Show minimap"]').click()
    page.wait_for_function("() => !document.querySelector('.okf-graph-minimap').hidden")
    page.evaluate("theme => window.OKFLoomTheme.setTheme(theme)", theme)
    page.wait_for_function(
        "theme => document.documentElement.dataset.theme === theme", arg=theme
    )
    snapshot = _graph_theme_snapshot(page)
    assert snapshot["baseNode"]["color"] == node_text, snapshot
    assert snapshot["minimapColours"], snapshot
    assert snapshot["capture"]["layoutSettled"] is True, snapshot
    assert snapshot["capture"]["suspended"] is False, snapshot


def test_graph_forced_colours_keep_system_palette_across_theme_event(
    server_url: str, page
) -> None:
    """Theme events never route forced-colour graphs through ordinary tokens."""
    page.emulate_media(forced_colors="active")
    _open_graph(page, server_url)
    page.evaluate("() => window.OKFLoomTheme.setTheme('technical-light')")
    page.wait_for_function("() => document.documentElement.dataset.theme === 'technical-light'")
    page.evaluate(
        """() => {
          const G = window.__okfLoomGraph;
          const cy = G.cy;
          const edge = G.cy.edges().first();
          G.interaction.dispatch({type: 'SELECT', id: edge.source().id(), source: 'test', quietUrl: true});
          G.tracePath(cy.getElementById(edge.source().id()), cy.getElementById(edge.target().id()));
        }"""
    )
    page.wait_for_function("() => window.__okfLoomGraph.cy.edges('.okf-path').length > 0")
    before = page.evaluate(
        """() => {
          const G = window.__okfLoomGraph, cy = G.cy;
          const css = value => {
            const el = document.createElement('span'); el.style.color = value;
            return el.style.color.replace(/\\s+/g, '');
          };
          return {
            forced: matchMedia('(forced-colors: active)').matches,
            body: document.body.hasAttribute('data-okf-forced-colors'),
            node: css(cy.nodes().first().style('color')),
            edge: css(cy.edges().first().style('line-color')),
            path: css(cy.edges('.okf-path').first().style('line-color')),
            selected: css(cy.nodes(':selected').first().style('border-color')),
            capture: window.okfGraphCaptureState(),
          };
        }"""
    )
    assert before["forced"] and before["body"], before

    page.evaluate("() => window.OKFLoomTheme.setTheme('swiss-dark')")
    page.wait_for_function("() => document.documentElement.dataset.theme === 'swiss-dark'")
    after = page.evaluate(
        """() => {
          const G = window.__okfLoomGraph, cy = G.cy;
          const css = value => {
            const el = document.createElement('span'); el.style.color = value;
            return el.style.color.replace(/\\s+/g, '');
          };
          return {
            forced: matchMedia('(forced-colors: active)').matches,
            body: document.body.hasAttribute('data-okf-forced-colors'),
            node: css(cy.nodes().first().style('color')),
            edge: css(cy.edges().first().style('line-color')),
            path: css(cy.edges('.okf-path').first().style('line-color')),
            selected: css(cy.nodes(':selected').first().style('border-color')),
            capture: window.okfGraphCaptureState(),
          };
        }"""
    )
    assert after["forced"] and after["body"], after
    assert after["node"] == before["node"], (before, after)
    assert after["edge"] == before["edge"], (before, after)
    assert after["path"] == before["path"], (before, after)
    assert after["selected"] == before["selected"], (before, after)
    assert after["capture"]["layoutSettled"] is True, after
    assert after["capture"]["appliedLayoutGeneration"] == before["capture"]["appliedLayoutGeneration"]
