"""Graph/Studio acquisition cost without changing their readiness contracts."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

from browser_support import copy_bundle, live_server
from test_studio_iter1_browser import page, _wait_for_studio, DEMO_BUNDLE


@pytest.fixture(scope='module')
def acquisition_server(tmp_path_factory):
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp('graph-acquisition') / 'bundle')
    with live_server(bundle) as base:
        yield base, bundle


@pytest.mark.parametrize('route', ['/__graph', '/', '/tables/orders'])
def test_graph_requests_are_shared_or_structure_only(acquisition_server, page, route):
    base, _ = acquisition_server
    requests = []
    page.on('request', lambda request: requests.append(request.url)
            if urlparse(request.url).path == '/__data/graph.json' else None)
    page.goto(base + route, wait_until='load')
    _wait_for_studio(page)
    page.wait_for_function('() => window.okfLoomStudio.state.graphReady')
    if route == '/__graph':
        page.wait_for_function('() => window.okfGraphCaptureState?.().initialized')
        assert [urlparse(url).query for url in requests] == ['view=structure']
        assert page.evaluate('() => !!window.okfGraphData && !window.okfLoomStudio.state.graph.bodies')
    else:
        # Index recency/comment badges and document connection chips need only
        # structure. No full body payload on either surface.
        assert [urlparse(url).query for url in requests] == ['view=structure']
        assert page.evaluate('() => !window.okfLoomStudio.state.graph.bodies')
    assert page.evaluate('() => document.documentElement.classList.contains("okf-theme-ready")')
    result = page.evaluate('''async () => {
        const handle = window.okfCaptureReadiness.start({target: location.pathname === '/__graph' ? 'graph' : 'document'});
        return await handle.promise;
    }''')
    assert result['screenshotAllowed'], result


@pytest.mark.parametrize('delayed_script', ['studio.js', 'graph.js'])
def test_shared_initial_acquisition_in_either_script_order(acquisition_server, page, delayed_script):
    base, _ = acquisition_server
    page.add_init_script('''(() => {
        const native = window.fetch.bind(window);
        window.graphFetchOwners = [];
        window.fetch = (url, options) => {
            if (String(url) === '/__data/graph.json?view=structure') window.graphFetchOwners.push(new Error().stack);
            return native(url, options);
        };
    })();''')

    def hold_script(route):
        response = route.fetch()
        html = response.text()
        pattern = r'<script\b[^>]*\bsrc="/__static/' + re.escape(delayed_script) + r'"[^>]*></script>'
        html, replaced = re.subn(pattern, '', html)
        assert replaced == 1
        route.fulfill(response=response, body=html)

    # Hold one consumer entirely until the other's real acquisition completed,
    # then execute the unchanged packaged script. Network delays alone do not
    # reorder deferred scripts in every browser.
    page.route('**/__graph', hold_script)
    page.goto(base + '/__graph', wait_until='load')
    if delayed_script == 'studio.js':
        page.wait_for_function('() => window.okfGraphCaptureState?.().initialized')
        page.add_script_tag(url=base + '/__static/studio.js', type='module')
    else:
        page.wait_for_function('() => window.okfLoomStudio?.state.graphReady')
        page.add_script_tag(url=base + '/__static/graph.js')
    page.wait_for_function('() => window.okfGraphCaptureState?.().initialized && window.okfLoomStudio?.state.graphReady')
    owners = page.evaluate('() => window.graphFetchOwners')
    assert len(owners) == 1
    first = 'graph.js' if delayed_script == 'studio.js' else 'studio.js'
    assert '/' + first + ':' in owners[0], owners


def test_plain_document_acquires_structure_only_when_palette_opens(tmp_path, page):
    (tmp_path / 'a.md').write_text('---\ntype: note\ntitle: Palette target\n---\nPlain body\n')
    with live_server(tmp_path) as base:
        requests = []
        page.on('request', lambda request: requests.append(request.url)
                if urlparse(request.url).path == '/__data/graph.json' else None)
        page.goto(base + '/a', wait_until='load')
        _wait_for_studio(page)
        page.wait_for_function('() => window.okfLoomStudio.state.commentsReady && window.okfLoomStudio.state.eventsReady')
        assert requests == []
        page.evaluate('() => window.okfLoomStudio.openPalette()')
        page.wait_for_function('() => window.okfLoomStudio.state.graphReady')
        assert [urlparse(url).query for url in requests] == ['view=structure']
        assert page.locator('.okf-palette__item', has_text='Palette target').count() == 1


@pytest.mark.parametrize('route', ['/', '/tables/orders'])
def test_bfcache_pageshow_restarts_discarded_structure_acquisition(acquisition_server, page, route):
    """Focused lifecycle proof: the first acquisition is held across pagehide,
    a case real Chromium BFCache admission may refuse with a network in flight."""
    base, _ = acquisition_server
    page.add_init_script('''(() => {
      const native = window.fetch.bind(window);
      window.structureAcquisitions = 0;
      window.fetch = (url, options) => {
        if (String(url) === '/__data/graph.json?view=structure') {
          if (++window.structureAcquisitions === 1) return new Promise(resolve => {
            window.releaseOldStructure = () => resolve(new Response(JSON.stringify(
              {nodes: [], bundle_revision: 'discarded-before-restore'}), {status: 200}));
          });
        }
        return native(url, options);
      };
    })();''')
    page.goto(base + route, wait_until='load')
    _wait_for_studio(page)
    assert page.evaluate('window.structureAcquisitions') == 1
    assert not page.evaluate('window.okfLoomStudio.state.graphReady')
    page.evaluate('''async () => {
      dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
      window.releaseOldStructure();
      await new Promise(resolve => setTimeout(resolve, 50));
      dispatchEvent(new PageTransitionEvent('pageshow', {persisted: false}));
    }''')
    assert not page.evaluate('window.okfLoomStudio.state.graphReady')
    assert page.evaluate('window.structureAcquisitions') == 1
    page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    page.wait_for_function('() => window.okfLoomStudio.state.graphReady')
    assert page.evaluate('window.structureAcquisitions') == 2
    assert page.evaluate("window.okfLoomStudio.state.graph.bundle_revision !== 'discarded-before-restore'")
    assert page.evaluate('window.okfLoomStudio.state.graph.nodes.length > 0')


def test_bfcache_plain_document_preserves_lazy_structure_rule(tmp_path, page):
    (tmp_path / 'a.md').write_text('---\ntype: note\n---\nPlain body\n')
    with live_server(tmp_path) as base:
        requests = []
        page.on('request', lambda request: requests.append(request.url)
                if urlparse(request.url).path == '/__data/graph.json' else None)
        page.goto(base + '/a', wait_until='load')
        _wait_for_studio(page)
        page.evaluate('''() => {
          dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
          dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        }''')
        page.wait_for_timeout(300)
        assert requests == []
        page.evaluate('() => window.okfLoomStudio.openPalette()')
        page.wait_for_function('() => window.okfLoomStudio.state.graphReady')
        assert [urlparse(url).query for url in requests] == ['view=structure']


def test_graph_refresh_publishes_latest_data_to_studio_without_second_fetch(acquisition_server, page):
    base, bundle = acquisition_server
    requests = []
    page.on('request', lambda request: requests.append(request.url)
            if urlparse(request.url).path == '/__data/graph.json' else None)
    page.goto(base + '/__graph', wait_until='load')
    _wait_for_studio(page)
    page.wait_for_function('() => window.okfGraphCaptureState?.().initialized && window.okfLoomStudio.state.graphReady')
    assert len(requests) == 1
    source = bundle / 'tables' / 'orders.md'
    original = source.read_text()
    try:
        source.write_text(original.replace('title: Orders', 'title: Shared refresh title'))
        page.wait_for_function('''() => window.okfLoomStudio.state.graph.nodes.some(
            node => node.data.id === 'tables/orders' && node.data.label === 'Shared refresh title')''', timeout=15000)
        assert len(requests) == 2
        assert page.evaluate('''() => window.__okfLoomGraph.cy.getElementById('tables/orders').data('label') === 'Shared refresh title' ''')
    finally:
        source.write_text(original)
