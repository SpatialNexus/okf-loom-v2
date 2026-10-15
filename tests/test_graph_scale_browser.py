"""One many-concept browser proof plus narrow lazy-read lifecycle checks."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from okf_loom import render
from test_graph_response import generated_bundle
from test_viewer_browser import live_server, page  # noqa: F401
from test_graph_acquisition_browser import _wait_for_studio

pytestmark = pytest.mark.browser


def capture(page):
    return page.evaluate("""async () => await window.okfCaptureReadiness.start({target:'graph'}).promise""")


def select(page, cid):
    page.evaluate("id => window.okfGraphInteraction.dispatch({type:'SELECT',id,source:'test'})", cid)


def test_live_1500_capture_ready_and_selected_body_is_lazy_full_view_html(tmp_path, page):
    bundle = generated_bundle(tmp_path, 1500, write=True)
    requests, held = [], []
    page.on('request', lambda request: requests.append(request.url)
            if '/__data/graph' in request.url else None)
    page.route('**/__data/graph-body?*', lambda route: held.append(route))
    with live_server(tmp_path) as base:
        page.goto(base + '/__graph', wait_until='load')
        _wait_for_studio(page)
        assert capture(page)['screenshotAllowed']
        assert page.evaluate('window.okfGraphCaptureState().payloadNodeCount') == 1500
        assert [urlparse(url).query for url in requests] == ['view=structure']
        assert page.evaluate('() => !window.okfLoomStudio.state.graph.bodies')
        # Hover cards and the palette contain identity/metrics, not body HTML.
        page.evaluate('window.okfLoomStudio.openPalette()')
        page.keyboard.press('Escape')
        assert len(requests) == 1
        cid = page.evaluate('window.__okfLoomGraph.cy.nodes()[0].id()')
        select(page, cid)
        page.wait_for_function("document.getElementById('detail-body').getAttribute('aria-busy') === 'true'")
        assert page.evaluate('window.okfGraphCaptureState().transitionPending')
        assert len(held) == 1
        expected = render.build_graph_data(bundle)['bodies'][cid]
        response = held[0].fetch()
        doc = response.json()
        assert doc['html'] == expected
        held[0].fulfill(response=response)
        assert capture(page)['screenshotAllowed']
        # Compare with the old inline/full path after the same internal-link
        # decoration. No enhancement blocks are in this small fixture body.
        expected_dom = page.evaluate(r"""html => {
          const root = document.createElement('div'); root.innerHTML=html;
          root.querySelectorAll('a[href]').forEach(a => {
            const id = a.getAttribute('href').replace(/^\//,'').replace(/\.md$/,'');
            a.setAttribute('href','#node-'+id); a.className='okf-internal';
            a.dataset.okfLinkConcept=id; a.removeAttribute('target');
          }); return root.innerHTML;
        }""", expected)
        assert page.locator('#detail-body').inner_html() == expected_dom
        assert page.locator('#detail-title').inner_text() == bundle.concepts[(cid,)].title
        other = page.evaluate('window.__okfLoomGraph.cy.nodes()[1].id()')
        select(page, other)
        page.wait_for_function('() => window.okfGraphCaptureState().transitionPending')
        page.wait_for_timeout(50)
        select(page, cid)
        assert capture(page)['screenshotAllowed']
        assert len(held) == 2  # selecting cached cid did not fetch it again
        held[1].fulfill(response=held[1].fetch())
        page.wait_for_timeout(50)
        assert page.locator('#detail-title').inner_text() == bundle.concepts[(cid,)].title
        assert page.locator('#detail-body').inner_html() == expected_dom


def test_graph_refusal_shows_server_json_message(tmp_path, page):
    generated_bundle(tmp_path, 2, write=True)
    message = "This bundle's graph is too large to display (22 MiB, limit 16 MiB)."
    page.route('**/__data/graph.json*', lambda route: route.fulfill(
        status=413, content_type='application/json',
        body=json.dumps({'ok': False, 'error': {'code': 'response_too_large', 'message': message}})))
    with live_server(tmp_path) as base:
        page.goto(base + '/__graph', wait_until='load')
        page.wait_for_function("() => document.querySelector('.okf-graph-loading--error')")
        assert message in page.locator('.okf-graph-loading--error').inner_text()
        assert page.evaluate('window.okfGraphCaptureState().terminalPhase') == 'graph-http'


def test_body_queue_is_bounded_latest_wins_and_pagehide_fences_ignored_abort(tmp_path, page):
    generated_bundle(tmp_path, 8, write=True)
    # A deliberately abort-ignoring transport proves both the admission bound
    # and generation fencing, not just native fetch's AbortError behaviour.
    page.add_init_script("""(() => {
      const native=window.fetch.bind(window); window.bodyReads=[];
      window.fetch=(url, opts) => {
        if (!String(url).startsWith('/__data/graph-body?')) return native(url,opts);
        return new Promise(resolve => window.bodyReads.push({url:String(url), resolve}));
      };
      window.releaseBody = (i, text) => {
        const read=window.bodyReads[i], url=new URL(read.url,location.href);
        read.resolve(new Response(JSON.stringify({id:url.searchParams.get('id'),
          bundle_revision:url.searchParams.get('revision'), html:'<p>'+text+'</p>',metadata_html:''})));
      };
    })();""")
    with live_server(tmp_path) as base:
        page.goto(base + '/__graph', wait_until='load')
        assert capture(page)['screenshotAllowed']
        for i in range(6):
            select(page, f'node-{i:05d}')
        assert page.evaluate('window.bodyReads.length') == 2
        page.evaluate("window.releaseBody(1,'stale second')")
        page.wait_for_function('() => window.bodyReads.length === 3')
        assert page.evaluate("new URL(window.bodyReads[2].url,location.href).searchParams.get('id')") == 'node-00005'
        page.evaluate("window.releaseBody(2,'latest body')")
        assert capture(page)['screenshotAllowed']
        assert page.locator('#detail-body').inner_text() == 'latest body'
        page.evaluate("window.releaseBody(0,'stale first')")
        page.wait_for_timeout(50)
        assert page.locator('#detail-body').inner_text() == 'latest body'
        select(page, 'node-00006')
        page.wait_for_function('() => window.bodyReads.length === 4')
        page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})); window.releaseBody(3,'hidden stale');")
        page.wait_for_timeout(50)
        assert 'hidden stale' not in page.locator('#detail-body').inner_text()
        page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
        page.wait_for_function('() => window.bodyReads.length === 5')
        page.evaluate("window.releaseBody(4,'restored body')")
        assert capture(page)['screenshotAllowed']
        assert page.locator('#detail-body').inner_text() == 'restored body'
