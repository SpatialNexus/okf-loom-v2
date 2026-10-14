"""Focused current-v2 graph consumer proofs for the Ledger palettes.

These browser checks deliberately exercise the real graph page instead of
asserting only source strings: graph.js consumes theme.js events, resolves the
active palette's tokens, paints Cytoscape/minimap state from them, recolours
lens encodings from a muted palette-derived ramp, and the mobile graph chrome
shares the page with the non-modal Appearance overlay.
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

PALETTES = ("bone", "slate", "clay", "plum")

# The Ledger graph roles per palette (DESIGN.md / tokens.css). graph.js must
# paint exactly these resolved token values; there is no per-theme JS table.
# "edge" is --graph-edge lifted toward --text-3 until it clears 3:1 on the
# canvas (WCAG 1.4.11): the raw edge tokens sit near 2.5:1.
LEDGER_GRAPH = {
    "bone": {"node": "rgb(210,202,189)", "stroke": "rgb(176,166,150)", "label": "rgb(29,26,22)",
             "edge": "rgb(146,137,124)", "halo": "rgb(246,243,237)", "accent": "rgb(163,87,58)",
             "raised": "rgb(227,221,210)", "ink": "rgb(29,26,22)"},
    "slate": {"node": "rgb(44,50,59)", "stroke": "rgb(59,66,76)", "label": "rgb(227,231,235)",
              "edge": "rgb(91,99,108)", "halo": "rgb(16,18,21)", "accent": "rgb(95,135,180)",
              "raised": "rgb(30,34,42)", "ink": "rgb(227,231,235)"},
    "clay": {"node": "rgb(51,45,41)", "stroke": "rgb(67,60,56)", "label": "rgb(236,231,227)",
             "edge": "rgb(106,98,92)", "halo": "rgb(18,16,16)", "accent": "rgb(181,106,74)",
             "raised": "rgb(34,30,28)", "ink": "rgb(236,231,227)"},
    "plum": {"node": "rgb(49,45,58)", "stroke": "rgb(67,62,76)", "label": "rgb(232,230,236)",
             "edge": "rgb(101,96,111)", "halo": "rgb(17,16,19)", "accent": "rgb(152,120,162)",
             "raised": "rgb(33,31,39)", "ink": "rgb(232,230,236)"},
}


def _set_palette(page, palette: str) -> None:
    page.evaluate("p => window.OKFLoomTheme.setTheme(p)", palette)
    page.wait_for_function("p => document.documentElement.dataset.theme === p", arg=palette)
    page.wait_for_function("""() => {
      const state = window.okfGraphCaptureState && window.okfGraphCaptureState();
      return state && state.layoutSettled && !state.transitionPending;
    }""")


def _open_mobile_graph(page, server_url: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script(
        "try { localStorage.setItem('okfGraphTourDone', '1'); "
        "localStorage.removeItem('okf-loom.appearance'); } catch (e) {}"
    )
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1"
    )


@pytest.mark.parametrize("width", [320, 390])
def test_mobile_graph_appearance_overlay_has_topmost_pointer_and_keyboard_controls(
    server_url: str, page, width: int
) -> None:
    """The mobile graph chrome stays below the fixed, non-modal Appearance menu."""
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
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "system"
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "bone"

    geometry = page.evaluate(
        """() => {
          const menu = document.getElementById('okf-appearance-menu');
          const option = menu.querySelector('input[data-okf-set="theme"][data-okf-val="slate"]');
          const r = menu.getBoundingClientRect();
          const o = option.closest('label').getBoundingClientRect();
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
    # The five-option Ledger menu is taller than the old one; theme.js still
    # clamps it inside the 844px viewport.
    assert geometry["menu"]["bottom"] <= 844 + 0.5, geometry
    assert geometry["hitInsideMenu"], geometry
    assert all(z < geometry["menu"]["z"] for z in geometry["chromeZ"]), geometry
    assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry

    # Escape remains the menu's topmost-layer action and restores its trigger.
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('okf-appearance-menu').hidden")
    assert page.locator("#okf-theme").evaluate("el => document.activeElement === el")

    # Pointer activation reaches an option rather than graph chrome, and the
    # canvas follows the chosen palette.
    trigger.click()
    page.wait_for_function("() => !document.getElementById('okf-appearance-menu').hidden")
    page.locator('#okf-appearance-menu label[data-okf-choice="slate"]').click()
    expect(page.locator("html")).to_have_attribute("data-theme", "slate")
    page.wait_for_function(
        "node => window.__okfLoomGraph.cy.nodes().first().style('background-color').replace(/\\s+/g, '') === node",
        arg=LEDGER_GRAPH["slate"]["node"],
    )


_CSS_JS = """
  const css = value => {
    const el = document.createElement('span');
    el.style.color = value;
    return el.style.color.replace(/\\s+/g, '');
  };
"""


def _graph_theme_snapshot(page):
    return page.evaluate(
        """() => {
          const G = window.__okfLoomGraph;
          const cy = G.cy;""" + _CSS_JS + """
          const first = selector => cy.elements(selector).first();
          // A path node other than the selected start (whose 4px accent
          // stroke deliberately wins over the ink path mark).
          const node = cy.nodes('.okf-path').not(':selected').first();
          const baseNode = cy.nodes().not(':selected').not('.okf-path').not('.okf-hover')
            .not('.okf-orphan').not('.okf-hub').first();
          const hover = first('node.okf-hover');
          const hub = first('node.okf-hub');
          const selected = first('node:selected');
          const path = first('edge.okf-path');
          const multi = first('edge.okf-multi-edge');
          const plain = cy.edges().not('.okf-path').not(':selected').first();
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
            baseNode: style(baseNode, ['background-color', 'color', 'text-outline-color', 'border-color']),
            baseNodeBorderWidth: baseNode.style('border-width'),
            baseNodeIcon: decodeURIComponent(baseNode.data('iconUrl') || ''),
            pathNode: style(node, ['underlay-color', 'border-color']),
            hover: style(hover, ['underlay-color']),
            hub: style(hub, ['underlay-color']),
            hubCorner: hub && hub.length ? parseFloat(hub.style('underlay-corner-radius')) : null,
            selected: style(selected, ['border-color']),
            selectedWidth: selected && selected.length ? selected.style('border-width') : null,
            pathEdge: style(path, ['line-color', 'target-arrow-color']),
            plainEdge: style(plain, ['line-color']),
            multi: style(multi, ['text-border-color', 'text-background-color']),
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
    """A palette event repaints path, hover/hub, selection, bundles, glyphs and overview."""
    _open_graph(page, docs_server_url)
    _set_palette(page, "bone")

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
    bone = LEDGER_GRAPH["bone"]
    assert before["baseNode"]["background-color"] == bone["node"], before
    assert before["selected"]["border-color"] == bone["accent"], before

    _set_palette(page, "slate")
    after = _graph_theme_snapshot(page)
    slate = LEDGER_GRAPH["slate"]

    # The accent is the selection only; halos are the neutral raised surface
    # and the traced path is drawn in ink.
    assert after["selected"]["border-color"] == slate["accent"], after
    assert after["selectedWidth"] == "4px", after
    assert after["pathNode"]["underlay-color"] == slate["raised"], after
    assert after["pathNode"]["border-color"] == slate["ink"], after
    assert after["hover"]["underlay-color"] == slate["raised"], after
    assert after["hub"]["underlay-color"] == slate["raised"], after
    assert after["hubCorner"] == 0, after
    assert after["pathEdge"]["line-color"] == slate["ink"], after
    assert after["pathEdge"]["target-arrow-color"] == slate["ink"], after
    assert after["multi"]["text-border-color"] == slate["stroke"], after
    # Neutral record: fill, stroke, label and halo all follow the new palette.
    assert after["baseNode"]["background-color"] == slate["node"], after
    assert after["baseNode"]["border-color"] == slate["stroke"], after
    assert after["baseNode"]["color"] == slate["label"], after
    assert after["baseNode"]["text-outline-color"] == slate["halo"], after
    assert after["plainEdge"]["line-color"] == slate["edge"], after
    # Glyph SVG caches are regenerated in the new palette's icon ink.
    assert 'stroke="#e3e7eb"' in after["baseNodeIcon"], after["baseNodeIcon"][:200]
    assert 'stroke="#1d1a16"' in before["baseNodeIcon"], before["baseNodeIcon"][:200]
    assert after["minimapColours"] != before["minimapColours"], (
        "minimap pixels did not change with the palette event",
        before["minimapColours"], after["minimapColours"],
    )
    assert after["capture"]["layoutSettled"] is True, after
    assert after["layout"] == before["layout"], (before, after)
    assert after["capture"]["appliedLayoutGeneration"] == before["capture"]["appliedLayoutGeneration"]
    assert after["capture"]["suspended"] is False
    assert after["capture"].get("terminalCode") is None
    assert after["capture"]["initialized"] is True
    assert after["capture"]["nodeCount"] == before["capture"]["nodeCount"]


@pytest.mark.parametrize("palette", PALETTES)
def test_every_ledger_palette_paints_neutral_nodes_from_tokens(
    docs_server_url: str, page, palette: str
) -> None:
    """Each palette reaches Cytoscape, the overview map and the info panel;
    the default Map lens never colour-codes content types."""
    _open_graph(page, docs_server_url)
    page.locator('button[aria-label="Show minimap"]').click()
    page.wait_for_function("() => !document.querySelector('.okf-graph-minimap').hidden")
    _set_palette(page, palette)
    snap = page.evaluate(
        """() => {
          const cy = window.__okfLoomGraph.cy;""" + _CSS_JS + """
          const root = getComputedStyle(document.documentElement);
          const token = name => css(root.getPropertyValue(name).trim());
          const plain = cy.nodes().not('.okf-orphan').not('.okf-hub').not(':selected');
          const fills = new Set(cy.nodes().map(n => css(n.style('background-color'))));
          const types = new Set(cy.nodes().map(n => n.data('type')));
          const density = document.querySelector('.okf-graph-density');
          const canvas = document.querySelector('.okf-graph-minimap canvas');
          const px = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
          let painted = 0; for (let i = 3; i < px.length; i += 4) if (px[i]) painted++;
          return {
            lens: window.__okfLoomGraph.getState().lens,
            fills: [...fills], typeCount: types.size,
            stroke: [...new Set(plain.map(n => css(n.style('border-color'))))],
            strokeWidth: [...new Set(plain.map(n => n.style('border-width')))],
            label: css(plain.first().style('color')),
            edge: css(cy.edges().not(':selected').first().style('line-color')),
            edgeContrast: (() => { const probe = document.createElement('canvas').getContext('2d');
              const rgb = c => { probe.fillStyle = '#000'; probe.fillStyle = c; const h = probe.fillStyle;
                return h[0] === '#' ? [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)) : h.match(/[\\d.]+/g).slice(0, 3).map(Number); };
              const lin = v => { v /= 255; return v <= .04045 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
              const lum = c => .2126 * lin(c[0]) + .7152 * lin(c[1]) + .0722 * lin(c[2]);
              const a = lum(rgb(cy.edges().not(':selected').first().style('line-color')));
              const b = lum(rgb(root.getPropertyValue('--graph-bg').trim()));
              return (Math.max(a, b) + .05) / (Math.min(a, b) + .05); })(),
            tokens: {node: token('--graph-node'), stroke: token('--border-strong'),
              label: token('--graph-label'), edge: token('--graph-edge'), accent: token('--accent')},
            infoEdge: density ? css(getComputedStyle(density).borderLeftColor) : null,
            infoEdgeWidth: density ? getComputedStyle(density).borderLeftWidth : null,
            minimapPainted: painted,
          };
        }"""
    )
    expected = LEDGER_GRAPH[palette]
    assert snap["lens"] == "map", snap
    assert snap["typeCount"] > 1, snap
    # Neutral round records: one fill for every type.
    assert snap["fills"] == [expected["node"]] == [snap["tokens"]["node"]], snap
    assert snap["stroke"] == [expected["stroke"]], snap
    assert snap["strokeWidth"] == ["1.5px"], snap
    assert snap["label"] == expected["label"] == snap["tokens"]["label"], snap
    assert snap["edge"] == expected["edge"], snap
    assert snap["edgeContrast"] >= 3, snap
    # The info panel carries the accent's left edge.
    assert snap["infoEdge"] == expected["accent"] == snap["tokens"]["accent"], snap
    assert snap["infoEdgeWidth"] == "2px", snap
    assert snap["minimapPainted"] > 0, snap


# Muted-ramp audit of the canvas and its legend: every lens fill keeps the
# glyph ink >= 4.5:1, stays low-chroma (OKLab chroma), and the legend swatches
# are the very colours on the canvas.
_LENS_AUDIT_JS = """() => {
  const G = window.__okfLoomGraph, cy = G.cy;
  const probe = document.createElement('canvas').getContext('2d');
  const rgb = c => { probe.fillStyle = '#000'; probe.fillStyle = String(c).trim();
    const s = probe.fillStyle;
    if (s[0] === '#') return [1, 3, 5].map(i => parseInt(s.slice(i, i + 2), 16));
    return s.match(/[\\d.]+/g).slice(0, 3).map(Number); };
  const lin = v => { v /= 255; return v <= .04045 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
  const lum = c => .2126 * lin(c[0]) + .7152 * lin(c[1]) + .0722 * lin(c[2]);
  const ratio = (a, b) => { a = lum(a); b = lum(b); return (Math.max(a, b) + .05) / (Math.min(a, b) + .05); };
  const chroma = c => { const [r, g, b] = c.map(lin);
    const l = Math.cbrt(.4122214708*r + .5363325363*g + .0514459929*b);
    const m = Math.cbrt(.2119034982*r + .6806995451*g + .1073969566*b);
    const s = Math.cbrt(.0883024619*r + .2817188376*g + .6299787005*b);
    const A = 1.9779984951*l - 2.4285922050*m + .4505937099*s, B = .0259040371*l + .7827717662*m - .8086757660*s;
    return Math.hypot(A, B); };
  const key = c => rgb(c).join(',');
  const root = getComputedStyle(document.documentElement);
  const icon = rgb(root.getPropertyValue('--graph-node-icon'));
  const fills = {};
  cy.nodes().forEach(n => { fills[key(n.data('color'))] = rgb(n.data('color')); });
  const swatches = [...document.querySelectorAll('.okf-graph-legend__swatch[data-swatch]')]
    .filter(s => s.dataset.swatch !== 'transparent').map(s => key(s.dataset.swatch));
  const painted = [...new Set(cy.nodes().map(n => key(n.style('background-color'))))];
  return {
    lens: G.getState().lens, colorMode: G.getState().colorMode,
    fills: Object.keys(fills), painted, swatches,
    minIconContrast: Math.min(...Object.values(fills).map(c => ratio(c, icon))),
    maxChroma: Math.max(...Object.values(fills).map(chroma)),
    neutral: key(root.getPropertyValue('--graph-node')),
    canvas: key(root.getPropertyValue('--graph-bg')),
    digestBars: [...document.querySelectorAll('#detail-empty .okf-lens-summary__bar[data-community]')]
      .map(b => b.style.background ? key(getComputedStyle(b).backgroundColor) : null),
  };
}"""


def _apply_lens(page, lens: str) -> None:
    page.evaluate("l => window.__okfLoomGraph.applyLens(l)", lens)
    page.wait_for_function("l => window.__okfLoomGraph.getState().lens === l", arg=lens)
    page.wait_for_function("() => window.okfGraphCaptureState().layoutSettled")


@pytest.mark.parametrize("palette", PALETTES)
def test_lenses_use_a_muted_palette_ramp_reapplied_on_every_change(
    docs_server_url: str, page, palette: str
) -> None:
    """Themes, Recent and Bridges colour from a muted ramp derived from the
    active palette; the ramp, legend and digest bars follow lens switches and
    palette changes, and Map returns to neutral nodes."""
    _open_graph(page, docs_server_url)
    start = "slate" if palette == "bone" else "bone"
    _set_palette(page, start)
    _apply_lens(page, "themes")
    first = page.evaluate(_LENS_AUDIT_JS)
    # Switch palette WITH the lens on: the encoding is recoloured, not kept.
    _set_palette(page, palette)
    themes = page.evaluate(_LENS_AUDIT_JS)
    assert themes["colorMode"] == "community", themes
    assert len(themes["fills"]) >= 3, themes
    assert set(themes["fills"]).isdisjoint(first["fills"]), (first, themes)
    assert themes["painted"] and set(themes["painted"]) <= set(themes["fills"]), themes
    assert set(themes["fills"]) <= set(themes["swatches"]), themes
    assert themes["minIconContrast"] >= 4.5, themes
    # Muted: the categorical hues stay low-chroma. On Bone's paper lightness
    # the dark palettes' chroma would read as pastel, so it steps lower.
    assert themes["maxChroma"] <= (0.04 if palette == "bone" else 0.055), themes
    bars = [bar for bar in themes["digestBars"] if bar]
    assert bars and set(bars) <= set(themes["swatches"]), themes

    for lens in ("recent", "bridges"):
        _apply_lens(page, lens)
        audit = page.evaluate(_LENS_AUDIT_JS)
        assert audit["lens"] == lens, audit
        assert audit["minIconContrast"] >= 4.5, (lens, audit)
        assert audit["maxChroma"] <= 0.1, (lens, audit)
        assert audit["swatches"], (lens, audit)
        assert set(audit["painted"]) <= set(audit["fills"]), (lens, audit)
        if lens == "recent":
            # Undated concepts are hollow (the canvas colour) - one state
            # never confusable with a dated step - and the key says so.
            assert audit["canvas"] in audit["swatches"], audit

    _apply_lens(page, "map")
    neutral = page.evaluate(_LENS_AUDIT_JS)
    assert neutral["fills"] == [neutral["neutral"]], neutral
    assert neutral["swatches"] == [], neutral
    assert all(bar is None for bar in neutral["digestBars"]), neutral


def test_graph_chrome_is_square_and_starting_points_are_hairline_rows(
    docs_server_url: str, page
) -> None:
    """Ledger chrome: no rounded graph chrome, only --shadow-pop elevation,
    ranked starting points as hairline rows with a 2px bar, no empty strip."""
    _open_graph(page, docs_server_url)
    _set_palette(page, "bone")
    page.evaluate("() => document.querySelectorAll('.okf-detail details').forEach(d => { d.open = true; })")
    proof = page.evaluate(
        """() => {""" + _CSS_JS + """
          const scope = [...document.querySelectorAll(
            '.okf-graph-tools, .okf-graph-tools *, #okf-graph *, #okf-detail, #okf-detail *')];
          const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
          const rounded = scope.filter(el => visible(el) && !['CANVAS'].includes(el.tagName)
            && getComputedStyle(el).borderRadius.split(' ').some(v => parseFloat(v) > 0))
            .map(el => el.className || el.tagName);
          const pop = getComputedStyle(document.documentElement).getPropertyValue('--shadow-pop').trim();
          const popProbe = document.createElement('div'); popProbe.style.boxShadow = pop;
          document.body.appendChild(popProbe);
          const popShadow = getComputedStyle(popProbe).boxShadow; popProbe.remove();
          const shadows = scope.filter(el => visible(el)).map(el => getComputedStyle(el).boxShadow)
            .filter(s => s !== 'none' && s !== popShadow);
          const rows = [...document.querySelectorAll('#detail-empty .okf-lens-summary__row')];
          const bars = rows.map(r => r.querySelector('.okf-lens-summary__bar')).filter(Boolean);
          const orphans = document.querySelector('.okf-graph-orphans');
          const tools = document.querySelector('.okf-graph-tools');
          const head = tools.querySelector('h2').getBoundingClientRect();
          const field = tools.querySelector('input').getBoundingClientRect();
          return {
            rounded, shadows,
            rowCount: rows.length,
            rowFills: [...new Set(rows.map(r => css(getComputedStyle(r).backgroundColor)))],
            barHeights: [...new Set(bars.map(b => b.getBoundingClientRect().height))],
            notes: [...new Set(rows.map(r => getComputedStyle(r.querySelector('.okf-lens-summary__note')).fontFamily))],
            orphanVisible: !!orphans && visible(orphans),
            orphanCount: orphans ? orphans.querySelectorAll('[role=option]').length : 0,
            headCentre: head.top + head.height / 2, fieldCentre: field.top + field.height / 2,
          };
        }"""
    )
    assert proof["rounded"] == [], proof["rounded"][:10]
    assert proof["shadows"] == [], proof["shadows"][:10]
    assert proof["rowCount"] >= 5, proof
    assert proof["rowFills"] == ["rgba(0,0,0,0)"], proof
    assert proof["barHeights"] == [2], proof
    assert all("Azeret Mono" in family for family in proof["notes"]), proof
    # The orphan shelf exists only when there are orphans (no empty strip).
    assert proof["orphanVisible"] == (proof["orphanCount"] > 0), proof
    # "Find in graph" sits on its field's centre line.
    assert abs(proof["headCentre"] - proof["fieldCentre"]) <= 2, proof


_SELECTED_LENS_PREVIEW_JS = """() => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', {willReadFrequently: true});
  const rgb = (c) => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = c;
    cx.fillRect(0, 0, 1, 1); const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const lum = (c) => { const v = c.slice(0, 3).map(x => x / 255)
    .map(x => x <= .04045 ? x / 12.92 : Math.pow((x + .055) / 1.055, 2.4));
    return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]; };
  const ratio = (a, b) => { a = lum(a); b = lum(b); return (Math.max(a, b) + .05) / (Math.min(a, b) + .05); };
  const over = (fg, alpha, bg) => fg.slice(0, 3).map((v, i) => v * alpha + bg[i] * (1 - alpha));
  const choice = document.querySelector('.okf-signal__seg-item input:checked + .okf-lens-choice');
  const svg = choice.querySelector('.okf-lens-preview');
  const inset = rgb(getComputedStyle(svg).backgroundColor);
  const lines = [...svg.querySelectorAll('line')].map(l => {
    const cs = getComputedStyle(l), ink = rgb(cs.stroke);
    return ratio(over(ink, ink[3] * Number(cs.opacity), inset), inset);
  });
  const nodes = [...svg.querySelectorAll('circle:not(.okf-preview-ring)')].map(c => {
    const cs = getComputedStyle(c), ink = rgb(cs.fill), stroke = rgb(cs.stroke);
    return Math.max(ratio(ink, inset), ratio(stroke, inset));
  });
  const cs = getComputedStyle(choice);
  return {theme: document.documentElement.dataset.theme, lens: choice.previousElementSibling.value,
    line: Math.min(...lines), node: Math.min(...nodes),
    fill: cs.backgroundColor, bar: cs.borderLeftColor, barWidth: cs.borderLeftWidth,
    accent: rgb(getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()),
    selectedCue: getComputedStyle(choice, '::after').content};
}"""


def test_selected_lens_preview_stays_legible_in_every_palette_and_forced_colours(
    server_url: str, page
) -> None:
    """The selected lens is a pressed record (2px accent bar, no accent
    fill) whose preview picture stays legible on its own inset in every
    palette; forced colours keep a textual Selected cue."""
    _open_graph(page, server_url)
    page.evaluate("() => { document.querySelector('.okf-signal__lenses').open = true; }")
    for palette in PALETTES:
        _set_palette(page, palette)
        seen = page.evaluate(_SELECTED_LENS_PREVIEW_JS)
        # WCAG 1.4.11 asks 3:1 of meaningful graphics; the ink gives at
        # least 4.5:1 on the band inset in every palette.
        assert seen["line"] >= 4.5 and seen["node"] >= 4.5, (palette, seen)
        assert seen["fill"] in ("rgba(0, 0, 0, 0)", "transparent"), (palette, seen)
        assert seen["barWidth"] == "2px", (palette, seen)
        assert seen["bar"].replace(" ", "") == "rgb({},{},{})".format(*seen["accent"][:3]), (palette, seen)

    page.emulate_media(forced_colors="active")
    page.wait_for_function("() => matchMedia('(forced-colors: active)').matches")
    forced = page.evaluate(_SELECTED_LENS_PREVIEW_JS)
    assert forced["line"] >= 4.5 and forced["node"] >= 4.5, forced
    assert forced["selectedCue"] == '"Selected"', forced


_FORCED_GRAPH_SNAPSHOT_JS = """() => {
  const graph = window.__okfLoomGraph;
  const node = graph.cy.nodes(':selected').first();
  const edge = graph.cy.edges().not('.okf-path').not(':selected').first();
  const resolve = value => {
    const probe = document.createElement('span');
    probe.style.color = value;
    probe.style.position = 'fixed';
    probe.style.visibility = 'hidden';
    document.body.appendChild(probe);
    const result = getComputedStyle(probe).color;
    probe.remove();
    return result;
  };
  const rgb = value => {
    const match = String(value || '').match(/rgba?\\(([^)]+)/);
    return match ? match[1].split(',').slice(0, 3).map(v => v.trim()).join(',') : null;
  };
  const style = (element, name) => element && element.length ? rgb(element.style(name)) : null;
  return {
    theme: document.documentElement.dataset.theme,
    forced: matchMedia('(forced-colors: active)').matches,
    text: rgb(resolve('CanvasText')),
    canvas: rgb(resolve('Canvas')),
    highlight: rgb(resolve('Highlight')),
    nodeText: style(node, 'color'),
    nodeBorder: style(node, 'border-color'),
    nodeBackground: style(node, 'text-background-color'),
    nodeOutline: style(node, 'text-outline-color'),
    edgeLine: style(edge, 'line-color'),
    edgeArrow: style(edge, 'target-arrow-color'),
    edgeBackground: style(edge, 'text-background-color'),
  };
}"""


def test_forced_colors_graph_preserves_system_palette_on_theme_change(
    server_url: str, page
) -> None:
    """Under forced colours every palette event leaves the canvas on the
    system palette: node ink and edges in CanvasText, label plates in Canvas,
    the selected node's stroke in Highlight -- in Bone, Slate, Clay and Plum,
    never an ordinary palette token (port of the pre-Ledger release test)."""
    try:
        page.emulate_media(forced_colors="active", color_scheme="light")
    except Exception as exc:  # platform/browser inability only
        pytest.fail(f"forced-colors emulation failed on provisioned browser: {exc}")
    if not page.evaluate("() => window.matchMedia('(forced-colors: active)').matches"):
        pytest.fail("browser accepted forced-colors request but did not expose the media state")

    _open_graph(page, server_url)
    page.evaluate("""() => {
      const G = window.__okfLoomGraph;
      G.interaction.dispatch({type: 'SELECT', id: G.cy.nodes().first().id(), source: 'test', quietUrl: true});
    }""")
    page.wait_for_function("() => window.__okfLoomGraph.cy.nodes(':selected').length === 1")
    for palette in PALETTES:
        _set_palette(page, palette)
        snap = page.evaluate(_FORCED_GRAPH_SNAPSHOT_JS)
        assert snap["theme"] == palette and snap["forced"] is True, snap
        assert snap["nodeText"] == snap["text"], (palette, snap)
        assert snap["nodeBorder"] == snap["highlight"], (palette, snap)
        assert snap["nodeBackground"] == snap["canvas"], (palette, snap)
        assert snap["nodeOutline"] == snap["canvas"], (palette, snap)
        assert snap["edgeLine"] == snap["text"], (palette, snap)
        assert snap["edgeArrow"] == snap["text"], (palette, snap)
        assert snap["edgeBackground"] == snap["canvas"], (palette, snap)

    # A lens change re-runs the visual encoding; it must not hand the canvas
    # back to palette colours either.
    page.evaluate("() => window.__okfLoomGraph.applyLens('themes')")
    page.wait_for_function("() => window.__okfLoomGraph.getState().lens === 'themes'")
    lensed = page.evaluate(_FORCED_GRAPH_SNAPSHOT_JS)
    assert lensed["nodeText"] == lensed["text"], lensed
    assert lensed["edgeLine"] == lensed["text"], lensed
    assert lensed["edgeBackground"] == lensed["canvas"], lensed


def test_graph_forced_colours_keep_system_palette_across_theme_event(
    server_url: str, page
) -> None:
    """Palette events never route forced-colour graphs through ordinary tokens."""
    page.emulate_media(forced_colors="active")
    _open_graph(page, server_url)
    _set_palette(page, "bone")
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
    snapshot_js = """() => {
      const G = window.__okfLoomGraph, cy = G.cy;""" + _CSS_JS + """
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
    before = page.evaluate(snapshot_js)
    assert before["forced"] and before["body"], before

    for palette in ("slate", "plum"):
        _set_palette(page, palette)
        after = page.evaluate(snapshot_js)
        assert after["forced"] and after["body"], after
        assert after["node"] == before["node"], (palette, before, after)
        assert after["edge"] == before["edge"], (palette, before, after)
        assert after["path"] == before["path"], (palette, before, after)
        assert after["selected"] == before["selected"], (palette, before, after)
        assert after["capture"]["layoutSettled"] is True, after
        assert after["capture"]["appliedLayoutGeneration"] == before["capture"]["appliedLayoutGeneration"]
