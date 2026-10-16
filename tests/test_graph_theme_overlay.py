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
# Bone uses the contrast-repaired --text-3 (#675f55) for that lift.
LEDGER_GRAPH = {
    "bone": {"node": "rgb(210,202,189)", "stroke": "rgb(176,166,150)", "label": "rgb(29,26,22)",
             "edge": "rgb(145,136,123)", "halo": "rgb(246,243,237)", "accent": "rgb(163,87,58)",
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
          // Prefer the traced typed relation: the path's ink must win over its hue.
          const path = cy.edges('.okf-path.okf-rel-edge').length ? first('edge.okf-path.okf-rel-edge') : first('edge.okf-path');
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
          // A typed relation (primary type other than a plain link, D2-008)
          // with no reverse edge, so the traced path is that relation.
          const typed = cy.edges('.okf-rel-edge').filter(e => cy.edges().filter(o =>
            o.source().same(e.target()) && o.target().same(e.source())).empty()).first();
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


_SELECT_FIELD_JS = """selector => {
  const select = document.querySelector(selector);
  const root = getComputedStyle(document.documentElement);
  const resolve = value => {
    const probe = document.createElement('span');
    probe.style.color = value;
    document.body.appendChild(probe);
    const result = getComputedStyle(probe).color;
    probe.remove();
    return result;
  };
  const cs = getComputedStyle(select);
  return {
    disabled: select.disabled, focusVisible: select.matches(':focus-visible'),
    height: select.getBoundingClientRect().height, font: cs.fontFamily, size: cs.fontSize,
    radius: cs.borderTopLeftRadius, border: [cs.borderTopWidth, cs.borderTopStyle, cs.borderTopColor],
    background: cs.backgroundColor, color: cs.color, outline: [cs.outlineStyle, cs.outlineWidth, cs.outlineColor],
    shadow: cs.boxShadow,
    inputBorder: resolve(root.getPropertyValue('--input-border').trim()),
    inputBg: resolve(root.getPropertyValue('--input-bg').trim()),
    inputText: resolve(root.getPropertyValue('--input-text').trim()),
    rings: [resolve(root.getPropertyValue('--okf-accent').trim()), resolve(root.getPropertyValue('--focus').trim())],
  };
}"""

# Graph settings Depth and Group by, and the canvas focus bar's depth.
GRAPH_SELECTS = (
    '.okf-signal select[aria-label="Depth"]',
    '.okf-signal select[aria-label="Group by"]',
    '.okf-graph-focusbar select',
)


def _reveal_depth_select(page):
    select = page.locator('.okf-signal select[aria-label="Depth"]')
    select.wait_for(state="attached")
    details = page.get_by_role("navigation", name="Graph panes").get_by_role("button", name="Details", exact=True)
    if details.is_visible() and details.get_attribute("aria-pressed") != "true":
        details.click()
    page.evaluate("() => document.querySelectorAll('.okf-detail details').forEach(d => { d.open = true; })")
    select.scroll_into_view_if_needed()
    return select


def _explore_neighbourhood(page) -> None:
    page.evaluate("""() => {
      const G = window.__okfLoomGraph;
      G.interaction.dispatch({type: 'SELECT', id: G.cy.nodes().filter(n => n.degree() > 0).first().id(),
                              source: 'test', quietUrl: true});
    }""")
    _apply_lens(page, "focus")
    page.wait_for_function("() => !document.querySelector('.okf-signal select[aria-label=\"Depth\"]').disabled")


def test_graph_selects_are_ledger_fields(server_url: str, page) -> None:
    """Every graph select (Graph settings Depth and Group by, and the focus
    bar's neighbourhood depth) is a Ledger field in every palette: body type,
    --input tokens, a square 1px outline, no shadow, a 2px focus ring, a 44px
    coarse target and a system outline under forced colours."""
    _open_graph(page, server_url)
    _reveal_depth_select(page)
    _explore_neighbourhood(page)
    expect(page.locator(".okf-graph-focusbar select")).to_be_visible()
    for palette in PALETTES:
        _set_palette(page, palette)
        for selector in GRAPH_SELECTS:
            seen = page.evaluate(_SELECT_FIELD_JS, selector)
            where = (palette, selector, seen)
            assert seen["disabled"] is False, where
            assert "Public Sans" in seen["font"] and seen["size"] == "13px", where
            assert seen["radius"] == "0px" and seen["shadow"] == "none", where
            assert seen["border"][:2] == ["1px", "solid"] and seen["border"][2] == seen["inputBorder"], where
            assert seen["background"] == seen["inputBg"] and seen["color"] == seen["inputText"], where
            assert 32 <= seen["height"] < 44, where
    for selector in GRAPH_SELECTS:
        page.locator(selector).scroll_into_view_if_needed()
        page.keyboard.press("Tab")
        page.locator(selector).focus()
        focused = page.evaluate(_SELECT_FIELD_JS, selector)
        assert focused["focusVisible"], (selector, focused)
        assert focused["outline"][:2] == ["solid", "2px"] and focused["outline"][2] in focused["rings"], (selector, focused)

    page.emulate_media(forced_colors="active")
    page.wait_for_function("() => matchMedia('(forced-colors: active)').matches")
    for selector in GRAPH_SELECTS:
        forced = page.evaluate(_SELECT_FIELD_JS, selector)
        assert forced["border"][:2] == ["1px", "solid"], (selector, forced)
        assert forced["border"][2] != forced["background"], (selector, forced)

    coarse = page.context.browser.new_context(viewport={"width": 390, "height": 844},
                                              is_mobile=True, has_touch=True)
    try:
        phone = coarse.new_page()
        _open_graph(phone, server_url)
        assert phone.evaluate("matchMedia('(pointer: coarse)').matches")
        _reveal_depth_select(phone)
        for selector in GRAPH_SELECTS[:2]:
            assert phone.evaluate(_SELECT_FIELD_JS, selector)["height"] >= 44, selector
    finally:
        coarse.close()


_PANE_TABS_JS = """() => {
  const accent = (() => { const p = document.createElement('span');
    p.style.color = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; })();
  return {accent, tabs: [...document.querySelectorAll('.okf-graph-mobile-nav button')]
    .filter(b => b.getClientRects().length).map(b => { const cs = getComputedStyle(b);
      return {name: b.textContent.trim(), pressed: b.getAttribute('aria-pressed'), font: cs.fontFamily,
        weight: cs.fontWeight, background: cs.backgroundColor, radius: cs.borderTopLeftRadius,
        bottom: [cs.borderBottomWidth, cs.borderBottomColor], height: b.getBoundingClientRect().height}; })};
}"""


@pytest.mark.parametrize("width,height", [(768, 1024), (844, 390), (390, 844)])
def test_graph_pane_tabs_are_ledger_tabs_wherever_they_show(server_url: str, page, width, height) -> None:
    """graph.js shows the Graph/Concepts/Details pane tabs up to 900px, so
    tablet portrait and phone landscape get the same Ledger tabs as phones:
    no browser-default fill, Public Sans, and exactly one pressed tab marked by
    bold ink over a 2px accent underline, following the visible pane."""
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script("try { localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}")
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__okfLoomGraph?.layoutStats?.lastAppliedSeq >= 1")
    panes = page.get_by_role("navigation", name="Graph panes")
    expect(panes).to_be_visible()
    for name, pane in (("Details", "details"), ("Graph", "graph"), ("Concepts", "concepts")):
        panes.get_by_role("button", name=name, exact=True).click()
        expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", pane)
        seen = page.evaluate(_PANE_TABS_JS)
        tabs = seen["tabs"]
        assert [t["name"] for t in tabs] == ["Graph", "Concepts", "Details"], seen
        assert [t["name"] for t in tabs if t["pressed"] == "true"] == [name], seen
        for tab in tabs:
            assert "Public Sans" in tab["font"] and tab["radius"] == "0px", seen
            assert tab["background"] in ("rgba(0, 0, 0, 0)", "transparent"), seen
            assert tab["height"] >= 44, seen
            if tab["pressed"] == "true":
                assert tab["bottom"] == ["2px", seen["accent"]] and tab["weight"] == "700", seen
            else:
                assert tab["bottom"][0] == "1px" and tab["bottom"][1] != seen["accent"], seen


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


# ---- Forced colours: the system-colour canvas layer -------------------------
# Canvas pixels are never adjusted by the browser, so graph.js resolves the
# system colours itself. These proofs read the computed Cytoscape style of
# every element (window.__okfLoomGraph.cy is the diagnostic hook) and compare
# it with the system colours resolved in the same document.

_RGB_JS = """
  const rgb = value => {
    const m = String(value || '').match(/[\\d.]+/g);
    return m ? m.slice(0, 3).map(v => Math.round(Number(v))).join(',') : null;
  };
  const system = () => {
    const probe = document.createElement('span');
    probe.style.position = 'fixed';
    probe.style.forcedColorAdjust = 'none';
    document.body.appendChild(probe);
    const out = {};
    for (const [name, keyword] of Object.entries({canvas: 'Canvas', text: 'CanvasText',
        highlight: 'Highlight', gray: 'GrayText', link: 'LinkText',
        buttonText: 'ButtonText', buttonFace: 'ButtonFace'})) {
      probe.style.color = keyword;
      out[name] = rgb(getComputedStyle(probe).color);
    }
    probe.remove();
    return out;
  };
  const inkOf = (uri, attr) => {
    if (!uri || uri === 'none') return [];
    const svg = decodeURIComponent(uri);
    return [...svg.matchAll(new RegExp(attr + '="([^"]+)"', 'g'))].map(m => rgb(m[1]));
  };
"""

_FORCED_CANVAS_AUDIT_JS = """() => {
  const G = window.__okfLoomGraph, cy = G.cy;""" + _RGB_JS + """
  const STATE = ':selected, .okf-hover, .okf-search-match, .okf-path, .okf-focus-root, '
    + '.okf-tour-target, .okf-presence-halo, .dim, .okf-hover-dim, .okf-path-dim, .okf-search-nonmatch';
  const node = n => ({
    id: n.id(), state: n.is(STATE), cue: n.data('lensCue') || '',
    fill: rgb(n.style('background-color')), stroke: rgb(n.style('border-color')),
    strokeWidth: parseFloat(n.style('border-width')), strokeStyle: n.style('border-style'),
    shape: n.style('shape'), label: rgb(n.style('color')),
    plate: rgb(n.style('text-background-color')), plateOpacity: Number(n.style('text-background-opacity')),
    culled: n.hasClass('okf-label-culled'), textOpacity: Number(n.style('text-opacity')),
    halo: rgb(n.style('text-outline-color')), opacity: Number(n.style('opacity')),
    glyphInk: inkOf(n.data('iconUrl'), 'stroke'),
    brokenInk: inkOf(n.data('brokenBadgeUrl'), 'stroke').concat(inkOf(n.data('brokenBadgeUrl'), 'fill')),
    commentInk: inkOf(n.data('commentBadgeUrl'), 'stroke').concat(inkOf(n.data('commentBadgeUrl'), 'fill')),
  });
  const edge = e => ({
    id: e.id(), dim: e.is('.dim, .okf-hover-dim, .okf-path-dim, .okf-unrelated'),
    state: e.is(':selected, .okf-path'), chip: e.hasClass('okf-multi-edge'),
    line: rgb(e.style('line-color')), arrow: rgb(e.style('target-arrow-color')),
    width: parseFloat(e.style('width')), lineStyle: e.style('line-style'),
    label: rgb(e.style('color')), plate: rgb(e.style('text-background-color')),
    chipBorder: rgb(e.style('text-border-color')), opacity: Number(e.style('opacity')),
  });
  const legend = [...document.querySelectorAll('.okf-graph-legend__swatch[data-forced-cue]')]
    .map(s => s.getAttribute('data-forced-cue'));
  return {
    forced: matchMedia('(forced-colors: active)').matches,
    body: document.body.hasAttribute('data-okf-forced-colors'),
    lens: G.getState().lens, system: system(),
    nodes: cy.nodes().map(node), edges: cy.edges().map(edge), legend,
    legendTitle: (document.querySelector('.okf-graph-legend__title') || {}).textContent || '',
    indexCues: [...document.querySelectorAll('.okf-node-index__swatch[data-forced-cue]')].length,
  };
}"""


def _assert_system_canvas(audit) -> None:
    sys = audit["system"]
    assert audit["forced"] and audit["body"], audit
    assert sys["canvas"] != sys["text"], sys
    for n in audit["nodes"]:
        assert n["fill"] == sys["canvas"], n
        # A label culled under a higher-priority one hides its text and its
        # plate together; every painted label sits on an opaque Canvas plate.
        assert n["plate"] == sys["canvas"], n
        assert (n["plateOpacity"], n["textOpacity"]) == ((0, 0) if n["culled"] else (1, 1)), n
        assert n["halo"] == sys["canvas"] and n["opacity"] == 1, n
        assert n["glyphInk"] == [sys["text"]], n
        assert set(n["brokenInk"]) <= {sys["canvas"], sys["text"]}, n
        assert set(n["commentInk"]) <= {sys["canvas"], sys["text"]}, n
        if not n["state"]:
            assert n["stroke"] == sys["text"] and n["label"] == sys["text"], n
    for e in audit["edges"]:
        if e["dim"] and not e["state"]:
            # Dimmed context (including its count chip) recedes to GrayText
            # with a dotted line: a non-colour cue alongside the hue.
            assert e["line"] == e["arrow"] == e["label"] == sys["gray"], e
            assert e["lineStyle"] == "dotted", e
            continue
        if e["chip"]:
            assert e["label"] == sys["buttonText"] and e["plate"] == sys["buttonFace"], e
            assert e["chipBorder"] == sys["buttonText"], e
        if e["state"]:
            continue
        assert e["line"] == e["arrow"] == sys["text"], e
        assert e["width"] >= 3 and e["opacity"] == 1, e
        if not e["chip"]:
            assert e["label"] == sys["text"] and e["plate"] == sys["canvas"], e


def _forced_open(page, server_url: str, scheme: str) -> None:
    page.emulate_media(forced_colors="active", color_scheme=scheme)
    _open_graph(page, server_url)
    page.wait_for_function("() => document.body.hasAttribute('data-okf-forced-colors')")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_forced_colours_paint_every_element_from_system_colours(
    docs_server_url: str, page, scheme: str
) -> None:
    """Under forced colours every node is a Canvas disc with a CanvasText
    stroke, glyph and label on a Canvas plate, every edge is CanvasText,
    badges and count chips use system pairs, and lenses trade hue for shape
    (categories) or stroke weight (ordered values) on the canvas and in the
    key -- in the light and the dark system palette alike."""
    _forced_open(page, docs_server_url, scheme)
    audit = page.evaluate(_FORCED_CANVAS_AUDIT_JS)
    _assert_system_canvas(audit)
    assert any(n["brokenInk"] for n in audit["nodes"]), "fixture has a broken-link badge"
    assert {n["shape"] for n in audit["nodes"]} == {"ellipse"}, audit["nodes"][:3]
    assert {n["strokeWidth"] for n in audit["nodes"] if not n["state"]} >= {2}, audit["nodes"][:3]

    _apply_lens(page, "themes")
    themes = page.evaluate(_FORCED_CANVAS_AUDIT_JS)
    _assert_system_canvas(themes)
    shapes = {n["cue"]: n["shape"] for n in themes["nodes"]}
    assert len(set(shapes.values())) >= 3, shapes
    # One cue, one shape: equal categories share a shape, different ones never do.
    assert len(set(shapes.values())) == len(shapes), shapes
    assert themes["legendTitle"].startswith("Shape:"), themes["legendTitle"]
    assert themes["legend"] and set(themes["legend"]) <= set(shapes), (themes["legend"], shapes)
    assert themes["indexCues"] > 0, themes

    _apply_lens(page, "recent")
    recent = page.evaluate(_FORCED_CANVAS_AUDIT_JS)
    _assert_system_canvas(recent)
    widths = {n["cue"]: n["strokeWidth"] for n in recent["nodes"] if n["cue"].startswith("level-")}
    assert widths, recent["nodes"][:3]
    for cue, width in widths.items():
        # Ordered lenses: the stroke weight grows with the level.
        assert width == [1, 2.5, 4, 5.5, 7][int(cue[-1])], (cue, width)
    for n in recent["nodes"]:
        if n["cue"] == "undated" and not n["state"]:
            assert n["strokeStyle"] == "dotted", n
    assert recent["legendTitle"].startswith("Outline:"), recent["legendTitle"]
    assert {"level-4", "level-0", "undated"} <= set(recent["legend"]), recent["legend"]

    _apply_lens(page, "map")
    neutral = page.evaluate(_FORCED_CANVAS_AUDIT_JS)
    _assert_system_canvas(neutral)
    assert {n["shape"] for n in neutral["nodes"]} == {"ellipse"}, neutral["nodes"][:3]
    assert neutral["legend"] == [], neutral["legend"]

    # The overview map stays available and paints system ink only.
    page.get_by_role("button", name="Show minimap").click()
    minimap = page.locator(".okf-graph-minimap")
    expect(minimap).to_be_visible()
    painted = minimap.locator("canvas").evaluate(
        """(c, sys) => {
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          const seen = new Set();
          for (let i = 0; i < d.length; i += 4) if (d[i + 3] === 255) seen.add(d[i] + ',' + d[i + 1] + ',' + d[i + 2]);
          return {seen: [...seen], text: sys.text, highlight: sys.highlight};
        }""",
        audit["system"],
    )
    assert painted["text"] in painted["seen"] and painted["highlight"] in painted["seen"], painted


def test_forced_colour_states_differ_by_more_than_colour(docs_server_url: str, page) -> None:
    """Selection, path, search hits and the focus root are Highlight, and
    each also has its own stroke width/style, so no state is colour alone;
    dimmed context is GrayText with a dotted stroke."""
    _forced_open(page, docs_server_url, "light")
    cues_js = """() => {
      const cy = window.__okfLoomGraph.cy;""" + _RGB_JS + """
      const cue = el => el && el.length ? [rgb(el.style('border-color')), parseFloat(el.style('border-width')), el.style('border-style')] : null;
      const line = el => el && el.length ? [rgb(el.style('line-color')), parseFloat(el.style('width')), el.style('line-style')] : null;
      const plain = cy.nodes().not(':selected, .okf-path, .okf-search-match, .okf-focus-root, .okf-hover, .okf-bridge, .okf-orphan, .dim, .okf-path-dim, .okf-hover-dim, .okf-search-nonmatch').first();
      return {
        system: system(),
        base: cue(plain),
        selected: cue(cy.nodes(':selected').not('.okf-focus-root').first()),
        path: cue(cy.nodes('.okf-path').not(':selected').first()),
        search: cue(cy.nodes('.okf-search-match').not(':selected, .okf-path').first()),
        focus: cue(cy.nodes('.okf-focus-root').first()),
        dimNode: cue(cy.nodes('.okf-path-dim, .dim').not(':selected, .okf-path').first()),
        pathEdge: line(cy.edges('.okf-path').first()),
        dimEdge: line(cy.edges('.okf-path-dim, .okf-unrelated, .dim').not('.okf-path').first()),
      };
    }"""
    plain = page.evaluate(cues_js)
    page.evaluate(
        """() => {
          const G = window.__okfLoomGraph, cy = G.cy;
          const edge = cy.edges().filter(e => e.source().id() !== e.target().id()).first();
          G.interaction.dispatch({type: 'SELECT', id: edge.source().id(), source: 'test', quietUrl: true});
          G.tracePath(cy.getElementById(edge.source().id()), cy.getElementById(edge.target().id()));
        }"""
    )
    page.wait_for_function("() => window.__okfLoomGraph.cy.edges('.okf-path').length > 0")
    first = page.evaluate(cues_js)
    sys = first["system"]
    assert plain["base"] == [sys["text"], 2, "solid"], plain
    assert first["selected"] == [sys["highlight"], 6, "solid"], first
    assert first["path"] == [sys["highlight"], 5, "dotted"], first
    assert first["dimNode"] == [sys["gray"], 2, "dotted"], first
    assert first["pathEdge"] == [sys["highlight"], 7, "solid"], first
    assert first["dimEdge"][0] == sys["gray"] and first["dimEdge"][2] == "dotted", first

    page.evaluate("() => window.__okfLoomGraph.clearPath()")
    page.get_by_role("searchbox", name="Search graph concepts").fill("graph")
    page.wait_for_function("() => window.__okfLoomGraph.cy.nodes('.okf-search-match').not(':selected').length > 0")
    searched = page.evaluate(cues_js)
    assert searched["search"] == [sys["highlight"], 5, "dashed"], searched
    page.get_by_role("searchbox", name="Search graph concepts").fill("")

    _apply_lens(page, "focus")
    page.wait_for_function("() => window.__okfLoomGraph.cy.nodes('.okf-focus-root').length === 1")
    focused = page.evaluate(cues_js)
    assert focused["focus"] == [sys["highlight"], 7, "double"], focused

    # Every reader state differs from the plain node and from each other by
    # a non-colour property (stroke width/style), never by colour alone.
    shapes = [tuple(plain["base"][1:]), tuple(first["selected"][1:]), tuple(first["path"][1:]),
              tuple(searched["search"][1:]), tuple(focused["focus"][1:])]
    assert len(set(shapes)) == len(shapes), shapes


_RESTORE_SNAPSHOT_JS = """() => {
  const cy = window.__okfLoomGraph.cy;
  const NODE = ['background-color', 'border-color', 'border-width', 'border-style', 'shape', 'color',
    'text-background-color', 'text-background-opacity', 'text-outline-color', 'underlay-color',
    'underlay-opacity', 'underlay-padding', 'outline-width', 'opacity', 'width'];
  const EDGE = ['line-color', 'target-arrow-color', 'width', 'opacity', 'line-style', 'color',
    'text-background-color', 'text-background-opacity', 'text-border-color'];
  const out = {};
  cy.nodes().forEach(n => {
    out[n.id()] = NODE.map(p => n.style(p)).concat([n.data('iconUrl'), n.data('commentBadgeUrl'), n.data('brokenBadgeUrl'), n.data('color')]);
  });
  cy.edges().forEach(e => { out[e.id()] = EDGE.map(p => e.style(p)).concat([e.data('relColor')]); });
  out.__legend = [...document.querySelectorAll('.okf-graph-legend__swatch')].map(s => s.getAttribute('style'));
  out.__legendTitle = [...document.querySelectorAll('.okf-graph-legend__title')].map(t => t.textContent);
  out.__body = document.body.hasAttribute('data-okf-forced-colors');
  return out;
}"""


def _set_forced(page, active: bool) -> None:
    page.emulate_media(forced_colors="active" if active else "none")
    page.wait_for_function(
        "on => document.body.hasAttribute('data-okf-forced-colors') === on", arg=active
    )


def test_toggling_forced_colours_restores_the_ledger_palette_exactly(
    docs_server_url: str, page
) -> None:
    """Forced colours switched on and off at runtime -- including a palette
    change while they are on and a second entry -- hand every element back
    exactly the Ledger values it had: fills, strokes, labels, halos, edges,
    glyph and badge URIs, lens colours and the legend."""
    page.emulate_media(forced_colors="none", color_scheme="light")
    _open_graph(page, docs_server_url)
    _set_palette(page, "bone")
    page.evaluate(
        """() => {
          const G = window.__okfLoomGraph, cy = G.cy;
          const edge = cy.edges().first();
          G.interaction.dispatch({type: 'SELECT', id: edge.source().id(), source: 'test', quietUrl: true});
          G.tracePath(cy.getElementById(edge.source().id()), cy.getElementById(edge.target().id()));
        }"""
    )
    page.wait_for_function("() => window.__okfLoomGraph.cy.edges('.okf-path').length > 0")
    _apply_lens(page, "themes")
    bone = page.evaluate(_RESTORE_SNAPSHOT_JS)
    _set_palette(page, "slate")
    slate = page.evaluate(_RESTORE_SNAPSHOT_JS)
    _set_palette(page, "bone")
    assert page.evaluate(_RESTORE_SNAPSHOT_JS) == bone

    _set_forced(page, True)
    forced = page.evaluate(_RESTORE_SNAPSHOT_JS)
    changed = [key for key in bone if not key.startswith("__") and forced[key] != bone[key]]
    assert len(changed) == len([k for k in bone if not k.startswith("__")]), "forced colours repaint every element"

    # A palette change while forced colours own the canvas, then leaving them.
    _set_palette(page, "slate")
    _set_forced(page, False)
    assert page.evaluate(_RESTORE_SNAPSHOT_JS) == slate

    # A second entry must win over the palette rules appended on exit.
    _set_forced(page, True)
    assert page.evaluate(_RESTORE_SNAPSHOT_JS) != slate
    again = page.evaluate(_FORCED_CANVAS_AUDIT_JS)
    _assert_system_canvas(again)
    _set_forced(page, False)
    _set_palette(page, "bone")
    assert page.evaluate(_RESTORE_SNAPSHOT_JS) == bone
    capture = page.evaluate("() => window.okfGraphCaptureState()")
    assert capture["layoutSettled"] is True, capture
