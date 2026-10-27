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


@pytest.mark.parametrize('failure, message', [
    ('409', 'This bundle changed while the concept was loading. Try again to load the latest version.'),
    ('413', 'This concept is too large to display here. Open its page to read it.'),
    ('503', 'The server is busy or took too long to load this concept. Try again.'),
    ('network', 'Could not reach the server to load this concept. Check your connection and try again.'),
    ('abort', 'Loading this concept took too long or was interrupted. Try again.'),
    ('timeout', 'Loading this concept took too long or was interrupted. Try again.'),
])
def test_body_errors_are_readable_announced_and_keyboard_retryable(tmp_path, page, monkeypatch, failure, message):
    generated_bundle(tmp_path, 2, write=True)
    monkeypatch.setattr('browser_support.free_port', lambda: 9070)
    # A touch-enabled desktop viewport exercises the coarse-pointer target
    # without the unrelated compact-pane navigation changing the keyboard path.
    context = page.context.browser.new_context(has_touch=True, viewport={'width': 1280, 'height': 900})
    pg = context.new_page()
    try:
        pg.add_init_script('window.bodyFailure = ' + json.dumps(failure))
        pg.add_init_script("""(() => {
          const native = window.fetch.bind(window); window.bodyAttempts = 0;
          window.OKF_LOOM_TEST_DEADLINES = {graphBody: 80};
          window.fetch = (url, options) => {
            if (!String(url).startsWith('/__data/graph-body?')) return native(url, options);
            window.bodyAttempts++;
            if (window.bodyFailure === 'network') return Promise.reject(new TypeError('Failed to fetch'));
            if (window.bodyFailure === 'abort') return Promise.reject(new DOMException('raw abort text', 'AbortError'));
            if (window.bodyFailure === 'timeout') return new Promise((resolve, reject) => {
              options.signal.addEventListener('abort', () => reject(new DOMException('raw timeout text', 'AbortError')));
            });
            if (window.bodyFailure !== 'none') return Promise.resolve(new Response('{}', {status:Number(window.bodyFailure)}));
            return native(url, options);
          };
        })();""")
        with live_server(tmp_path, '--no-watch-ui') as base:
            pg.goto(base + '/__graph', wait_until='load')
            assert capture(pg)['screenshotAllowed']
            if pg.locator('.okf-graph-tour__skip').is_visible():
                pg.locator('.okf-graph-tour__skip').click()
            select(pg, 'node-00000')
            status = pg.locator('#detail-body-status[role="status"]')
            pg.wait_for_function("() => !!document.querySelector('#detail-body-status button')")
            assert status.locator('p').inner_text() == message
            assert 'HTTP' not in status.inner_text()
            assert not pg.locator('#detail-body').get_attribute('aria-busy')
            assert pg.evaluate('window.bodyAttempts') == (2 if failure == '409' else 1)
            result = pg.evaluate("""async () => await window.okfCaptureReadiness.start({target:'graph'}).promise
                .catch(error => ({code:error.code}))""")
            assert result == {'code': 'network-failure'}, result
            assert pg.evaluate('window.okfGraphCaptureState().terminalPhase') == 'graph-body'
            retry = status.get_by_role('button', name='Retry', exact=True)
            assert retry.evaluate("el => el.classList.contains('okf-btn') && el.tabIndex === 0")
            assert pg.evaluate("matchMedia('(pointer: coarse)').matches")
            bounds = retry.bounding_box()
            assert bounds['width'] >= 44 and bounds['height'] >= 44, bounds
            # Reach the control in native sequential focus order, then activate
            # it with the keyboard, not a synthetic click handler invocation.
            retry.focus()
            pg.keyboard.press('Shift+Tab')
            pg.keyboard.press('Tab')
            assert retry.evaluate('el => el === document.activeElement')
            pg.evaluate("window.bodyFailure = 'none'; window.OKF_LOOM_TEST_DEADLINES.graphBody = 10000")
            pg.keyboard.press('Enter')
            pg.wait_for_function("() => document.querySelector('#detail-body h2')?.textContent === 'Detail'")
            assert status.inner_text() == ''
            assert capture(pg)['screenshotAllowed']
    finally:
        context.close()
