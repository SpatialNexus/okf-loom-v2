"""First-frame projections and late web-font metrics on the graph canvas."""
from __future__ import annotations

import pytest

from test_viewer_browser import page, server_url  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("query", ["", "?lens=map", "?lens=unknown&selected=missing"])
def test_default_graph_projects_interaction_once_and_popstate_still_clears(page, server_url, query):
    page.add_init_script("""(() => {
        window.initialGraphRenders = [];
        let cytoscape;
        Object.defineProperty(window, 'cytoscape', {
            configurable:true, get:() => cytoscape,
            set:value => { cytoscape = new Proxy(value, {apply(target, self, args) {
                if (args[0]?.container) window.initialGraphLayout = args[0].layout.name;
                return Reflect.apply(target, self, args);
            }}); }
        });
        window.addEventListener('okf-loom:graph-interaction', event => {
            if (event.detail.source === 'render') window.initialGraphRenders.push(event.detail.snapshot);
        });
    })();""")
    page.goto(server_url + "/__graph" + query, wait_until="domcontentloaded")
    page.wait_for_function("() => window.okfGraphCaptureState?.().layoutSettled")
    initial = page.evaluate("""() => ({
        renders: window.initialGraphRenders,
        capture: window.okfGraphCaptureState(),
        starts: window.__okfLoomGraph.layoutStats.starts,
        seed: window.initialGraphLayout
    })""")
    assert len(initial["renders"]) == 1, initial
    assert initial["renders"][0]["lens"] == "map"
    assert initial["renders"][0]["selected"] is None
    assert initial["starts"] == 1
    assert initial["seed"] == "preset"
    assert initial["capture"]["layoutGeneration"] == initial["capture"]["appliedLayoutGeneration"]
    page.evaluate("""() => {
        const id = window.__okfLoomGraph.cy.nodes()[0].id();
        window.okfGraphInteraction.dispatch({type:'SELECT', id, source:'test'});
        history.replaceState({}, '', '/__graph');
        window.dispatchEvent(new PopStateEvent('popstate'));
    }""")
    snapshot = page.evaluate("() => window.okfGraphInteraction.snapshot()")
    assert snapshot["selected"] is None
    assert snapshot["lens"] == "map"
    assert snapshot["neighborhood"] is None


def test_late_ledger_fonts_remeasure_canvas_labels_and_redraw_once(page, server_url):
    # Embedded data removes the network gate: a usable graph must paint while
    # both local WOFF2 faces are still held, just like single-file/offline use.
    page.add_init_script("""(() => {
        window.BUNDLE = {
            bundle_revision:'font-swap', palette:{Synthetic:'#777'}, types:['Synthetic'], bodies:{},
            nodes:['n0','n1'].map((id, i) => ({data:{id, label:'iiiiiiii ' + i, type:'Synthetic'}})),
            edges:[{data:{id:'e0', source:'n0', target:'n1', label:'linked', multiplicity:2,
                relations:['r0','r1'].map(id => ({id, source:'n0', target:'n1',
                    direction:'outgoing', type:'link', label:'linked', kind:'relation'}))}}]
        };
        window.fontProof = {renders:0, updates:0, loadedAt:null, rendersAfterLoad:0};
        document.fonts.addEventListener('loadingdone', event => {
            if (event.fontfaces?.some(face => /Public Sans|Azeret Mono/.test(face.family)))
                window.fontProof.loadedAt = performance.now();
        });
        let cytoscape;
        Object.defineProperty(window, 'cytoscape', {
            configurable:true, get:() => cytoscape,
            set:value => { cytoscape = new Proxy(value, {apply(target, self, args) {
                const cy = Reflect.apply(target, self, args);
                if (args[0]?.container) {
                    window.fontProof.cy = cy;
                    const style = cy.style(), update = style.update;
                    style.update = function(...args) {
                        window.fontProof.updates++;
                        return update.apply(this, args);
                    };
                    cy.on('render', () => {
                        const proof = window.fontProof;
                        proof.renders++;
                        if (proof.loadedAt !== null) proof.rendersAfterLoad++;
                        if (proof.firstFontReady === undefined)
                            proof.firstFontReady = document.fonts.check('600 13px "Public Sans"');
                    });
                }
                return cy;
            }}); }
        });
    })();""")
    held = []
    page.route("**/*.woff2", lambda route: held.append(route))
    page.goto(server_url + "/__graph", wait_until="domcontentloaded")
    page.wait_for_function("() => window.fontProof.renders > 0")
    assert held, "The proof must delay actual vendored font requests"
    assert not page.evaluate("() => window.fontProof.firstFontReady")
    before = page.evaluate("""() => ({
        width:window.fontProof.cy.nodes()[0]._private.rscratch.labelWidth,
        updates:window.fontProof.updates,
        positions:window.fontProof.cy.nodes().map(n => n.position()),
        generation:window.okfGraphCaptureState().layoutGeneration
    })""")
    assert before["width"] > 0
    for route in held:
        route.continue_()
    page.unroute("**/*.woff2")
    page.wait_for_function("""() => document.fonts.status === 'loaded'
        && document.fonts.check('600 13px "Public Sans"')
        && document.fonts.check('500 10px "Azeret Mono"')
        && window.fontProof.rendersAfterLoad > 0""")
    after = page.evaluate("""() => {
        const cy = window.fontProof.cy, ctx = document.createElement('canvas').getContext('2d');
        return {
            updates:window.fontProof.updates,
            positions:cy.nodes().map(n => n.position()),
            generation:window.okfGraphCaptureState().layoutGeneration,
            faces:[...document.fonts].map(f => ({family:f.family, status:f.status})),
            labels:cy.elements().map(ele => {
                const font = ['font-style','font-weight','font-size','font-family']
                    .map(key => ele.pstyle(key).strValue).join(' ');
                ctx.font = font;
                const label = ele.isNode() ? ele.data('label') : ele.data('bundleLabel');
                return {font, cached:ele._private.rscratch.labelWidth,
                    fresh:Math.ceil(ctx.measureText(label).width)};
            })
        };
    }""")
    assert after["updates"] == before["updates"] + 1, after
    assert all(any(face["family"] == family and face["status"] == "loaded"
                   for face in after["faces"]) for family in ("Public Sans", "Azeret Mono")), after
    assert after["positions"] == before["positions"]
    assert after["generation"] == before["generation"]
    assert all(label["cached"] == label["fresh"] > 0 for label in after["labels"]), after
    assert "Public Sans" in after["labels"][0]["font"]
    assert "Azeret Mono" in after["labels"][-1]["font"]
    assert after["labels"][0]["cached"] != before["width"], repr(after)
    # An unrelated/repeated loading event does not restyle the whole graph.
    page.evaluate("() => document.fonts.dispatchEvent(new Event('loadingdone'))")
    assert page.evaluate("() => window.fontProof.updates") == after["updates"]
