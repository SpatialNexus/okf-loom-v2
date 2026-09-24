"""Real live, static and file:// single-file OKF 0.2 acceptance flows."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

import pytest
import yaml

from browser_support import sync_playwright, launch_chromium, expect, live_server, managed_server, free_port
from okf_loom.model import Bundle
from okf_loom.render import build_site
from test_okf02_viewer import write_fixture

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('mode', ['live', 'static', 'single-file'])
@pytest.mark.parametrize('width', [1280, 390])
def test_okf02_real_viewer_flow(tmp_path, mode, width):
    fm, body, path = write_fixture(tmp_path / 'bundle')
    with ExitStack() as stack:
        if mode == 'live':
            base = stack.enter_context(live_server(path.parents[1]))
            url = base + '/notes/review'
        else:
            out = tmp_path / 'site'
            build_site(Bundle.load(path.parents[1]), out, target=mode)
            if mode == 'static':
                port = free_port(); base = f'http://127.0.0.1:{port}'
                stack.enter_context(managed_server([sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1', '--directory', str(out)], base, tmp_path / 'http.log'))
                url = base + '/notes/review.html'
            else:
                url = (out / 'index.html').as_uri()
        pw = stack.enter_context(sync_playwright())
        browser = launch_chromium(pw); stack.callback(browser.close)
        context = browser.new_context(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
        page = context.new_page()
        errors, failures, requests = [], [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('requestfailed', lambda request: failures.append((request.url, request.failure)))
        page.on('request', lambda request: requests.append(request.url))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
        page.add_init_script("window.__okfBodyPatched = 0; window.addEventListener('okf-loom:bodyPatched', () => { window.__okfBodyPatched += 1; });")
        page.goto(url)
        if mode == 'single-file':
            page.wait_for_function('() => !!window.__okfLoomGraph && !!window.__okfLoomGraph.cy')
            page.evaluate('window.okfActivateGraphConcept("notes/review", "keyboard")')
        metadata = page.get_by_role('region', name='Trust, lifecycle and sources')
        expect(metadata).to_be_visible()
        expect(metadata).to_contain_text('Trust (advisory): human-reviewed')
        expect(metadata).to_contain_text('Lifecycle (advisory): draft')
        expect(metadata.locator('[data-okf-freshness]')).to_have_text('Stale')
        evidence = Path(os.environ.get('OKF02_EVIDENCE', '/tmp/okf02-ui-evidence'))
        evidence.mkdir(parents=True, exist_ok=True)
        # Claim joins by source id even though a Loom citation uses the same id.
        ref = page.get_by_role('link', name='Source alpha', exact=True)
        href_before = ref.get_attribute('href')
        ref.focus(); page.keyboard.press('Enter')
        expect(metadata.get_by_text('Usage count (not credibility)', exact=True).first).to_be_visible()
        expect(page.locator(href_before)).to_be_focused()
        assert len(context.pages) == 1, 'Source footnotes must not open a second viewer tab'
        expect(page.locator(href_before).locator('strong').first).to_be_in_viewport()
        expect(metadata).to_contain_text('Legacy timestamp (not generation time)')
        expect(metadata).to_contain_text('Metadata only — never executed')
        expect(metadata).to_contain_text('Ada')
        expect(metadata.get_by_role('link', name='External evidence')).to_have_attribute('href', 'https://example.org/evidence?a=1&b=2')
        assert metadata.locator('a[href^="javascript"], script, img, svg').count() == 0
        assert metadata.get_by_role('link', name='all repositories in project:atlas').count() == 0
        assert not page.evaluate('window.OKF_XSS || false')
        assert not any('DO-NOT-FETCH' in request or 'example.org/tracker' in request for request in requests)
        # Prove the real CDN enhancements, not only raw-source fallbacks.
        expect(page.locator('.mermaid svg').first).to_be_visible(timeout=60000)
        expect(page.locator('code.hljs').first).to_be_visible(timeout=60000)
        expect(page.locator('.katex').first).to_be_visible(timeout=60000)
        page.screenshot(path=str(evidence / f'{mode}-source-focus-{width}.png'))
        metadata.locator('details').evaluate('(el) => {el.open = false;}')
        page.evaluate('() => {window.scrollTo(0,0); const pane=document.getElementById("okf-detail"); if(pane)pane.scrollTop=0;}')
        page.screenshot(path=str(evidence / f'{mode}-{width}.png'))
        metadata.locator('summary').click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        if mode == 'live':
            # External canonical update goes through the real watcher/doc route;
            # no reload, artificial wait, or competing metadata fetch.
            fm['sources'].reverse(); fm['status'] = 'deprecated'; fm['verified'] = []
            patched_before = page.evaluate('window.__okfBodyPatched')
            path.write_text('---\n' + yaml.safe_dump(fm, sort_keys=False) + '---\n' + body)
            expect(metadata).to_contain_text('Lifecycle (advisory): deprecated', timeout=15000)
            expect(metadata).to_contain_text('Trust (advisory): unverified')
            # Only the frontmatter changed. Announcing a body patch for an unchanged
            # body rebound the re-rendered metadata links to the hover preview, and
            # with reduced motion (no hover delay) the click below then navigated
            # away from its own in-flight /__raw preview: the intermittent
            # ERR_ABORTED this flow used to record at 390px.
            assert page.evaluate('window.__okfBodyPatched') == patched_before == 0
            expect(ref).to_have_attribute('href', href_before)
            ref.focus(); page.keyboard.press('Enter')
            expect(page.locator(href_before)).to_contain_text('Local evidence')
            doc = page.request.get(base + '/__data/doc?id=notes/review').json()
            assert doc['annotation_projection']['complete']
            assert doc['metadata']['status'] == 'deprecated'
            page.screenshot(path=str(evidence / f'live-reordered-{width}.png'), full_page=True)
        source_link = metadata.get_by_role('link', name='Local evidence', exact=True)
        source_link.click()
        if mode == 'single-file':
            expect(page.locator('#detail-title')).to_have_text('Local evidence')
        else:
            expect(page.get_by_role('heading', name='Local evidence', exact=True).first).to_be_visible()
            expect(page.locator('body')).to_have_attribute('data-okf-renderers-status', 'ready')
            if mode == 'live':
                expect(page.locator('body')).to_have_attribute('data-okf-studio-status', 'ready')
                page.wait_for_function('() => window.okfLoomLive?._getLastDocRevForTest("notes/source")')
        # Navigate back through normal browser history.
        page.go_back()
        expect(page.get_by_role('region', name='Trust, lifecycle and sources')).to_be_visible()
        if mode == 'single-file':
            expect(page.locator('#detail-title')).to_have_text('Evidence review')
        assert not errors, errors
        # Closing a page deliberately aborts its endless SSE stream. Record
        # that lifecycle outcome, but reject every actual failed data/asset read.
        unexpected = [(url, reason) for url, reason in failures
                      if not (mode == 'live' and url.endswith(('/__events', '/__presence')) and reason == 'net::ERR_ABORTED')]
        assert not unexpected, unexpected
        (evidence / f'{mode}-{width}-diagnostics.json').write_text(json.dumps({'console_errors': errors, 'network_failures': failures, 'requests': requests}, indent=2))


def test_portable_metadata_and_legacy_adapter_in_browser(tmp_path):
    from okf_loom.render import _browser_passage_corpus
    _, _, path = write_fixture(tmp_path / 'bundle')
    corpus = _browser_passage_corpus(Bundle.load(path.parents[1]), [])
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            page = browser.new_page()
            page.goto('about:blank')
            page.add_script_tag(path=str(Path(__file__).parents[1] / 'scripts/okf_loom/viewer/static/portable-retrieval.js'))
            # about:blank has no SubtleCrypto; use a file secure context.
            blank = tmp_path / 'blank.html'; blank.write_text('<!doctype html><title>Portable proof</title>')
            page.goto(blank.as_uri())
            page.add_script_tag(path=str(Path(__file__).parents[1] / 'scripts/okf_loom/viewer/static/portable-retrieval.js'))
            result = page.evaluate('''async corpus => {
              const p = window.okfPortableRetrieval;
              const checked = await p.validate(corpus.manifest, corpus.chunks);
              const runtime = p.create(corpus.manifest, checked.passages, checked.edges);
              const rows = runtime.search({mode:'relation', relation:'derived_from'}).rows;
              const pack = runtime.context({concept:'notes/review'}, {neighbors:0});
              const tier = pack.items[0].metadata.trust_tier;
              corpus.manifest.concepts.forEach(c => { delete c.metadata; delete c.metadata_json; });
              await p.validate(corpus.manifest, corpus.chunks);
              return {tier, ids:rows.map(r=>r.concept.id), via:checked.edges[0].via};
            }''', corpus)
            assert result == {'tier':'human-reviewed', 'ids':['notes/review'], 'via':'sources'}
        finally:
            browser.close()


_PARITY_VARIANTS = {
    # name: extra frontmatter lines (raw YAML, so authored shapes stay exact)
    'plain': '',
    'machine': 'verified: {by: "process:ci", at: "2026-09-01T00:00:00Z"}\nstatus: deprecated\n'
               'stale_after: "2026-09-08T17:30:00+05:30"\n',
    'feb30': 'stale_after: "2026-02-30T00:00:00+00:00"\n',
    'feb30z': 'stale_after: "2026-02-30T00:00:00Z"\n',
    'hour24': 'stale_after: "2026-09-07T24:00:00+00:00"\n',
    'second60': 'stale_after: "2026-09-08T11:59:60+00:00"\n',
    'offset24': 'stale_after: "2026-09-08T12:00:00+24:00"\n',
    'dateonly': 'stale_after: 2026-09-08\n',
    'naive': 'stale_after: "2026-09-08T12:00:00"\n',
    'unquoted': 'stale_after: 2026-09-08T12:00:00Z\n',
    'micro': 'stale_after: "2026-09-08T12:00:00.000500Z"\n',
    'leap': 'status: stable\nstale_after: "2024-02-29T00:00:00Z"\n',
    'far': 'status: draft\nstale_after: "2099-12-31T23:59:59-23:59"\n',
    'nullexpiry': 'stale_after: null\n',
    'numexpiry': 'stale_after: 1789000000\n',
    'mixedcase': 'status: Draft\n',
    'longstatus': 'status: "' + 'b' * 39 + '\U0001F600 and more"\n',
    'numeric': 'status: 7\n',
    'nullstatus': 'status: null\n',
    'liststatus': 'status: [draft]\n',
    'hostile': 'status: "<img src=x onerror=window.OKF_XSS=1>"\n',
    'badverify': 'verified: {by: "human:ann", at: "not-a-date"}\n',
}


def test_search_advisory_tokens_match_python_in_browser(tmp_path):
    """U4 parity: the portable renderer (static, SPA, single-file) derives the
    same advisory tokens as live /__search from the same bounded projection,
    with strict instants (no Date.parse: 30 February, hour 24, second 60,
    date-only and naive values are invalid expiries in both)."""
    from datetime import datetime, timezone
    from okf_loom.render import _browser_passage_corpus
    from okf_loom.viewer.metadata import search_advisory_tokens
    _, _, path = write_fixture(tmp_path / 'bundle')
    for name, extra in _PARITY_VARIANTS.items():
        (path.parent / f'v-{name}.md').write_text(
            f'---\ntype: Note\ntitle: Variant {name}\n{extra}---\nVariant {name} body.\n', encoding='utf-8')
    corpus = _browser_passage_corpus(Bundle.load(path.parents[1]), [])
    utc = timezone.utc
    # Just before, exactly at, and 1 ms after the fixture expiry; then either
    # side of the far variant's 2100-01-01T23:58:59Z (authored at -23:59).
    nows = [datetime(2026, 9, 8, 11, 59, 59, 999000, tzinfo=utc), datetime(2026, 9, 8, 12, tzinfo=utc),
            datetime(2026, 9, 8, 12, 0, 0, 1000, tzinfo=utc), datetime(2100, 1, 1, 12, tzinfo=utc),
            datetime(2100, 1, 1, 23, 58, 59, tzinfo=utc)]
    concepts = {c['id']: c['metadata'] for c in corpus['manifest']['concepts']}
    expected = {cid: [[[kind, text, attention] for kind, text, attention in search_advisory_tokens(md, now)]
                      for now in nows] for cid, md in concepts.items()}
    # Pin the Python side so parity cannot pass vacuously.
    invalid = [['freshness', 'Freshness unknown (invalid expiry)', True]]
    for name in ('feb30', 'feb30z', 'hour24', 'second60', 'offset24', 'dateonly', 'naive', 'nullexpiry', 'numexpiry'):
        assert expected[f'notes/v-{name}'] == [invalid] * len(nows), name
    stale = ['freshness', 'Stale', True]
    assert expected['notes/v-unquoted'] == [[], [stale], [stale], [stale], [stale]]
    assert expected['notes/v-micro'] == [[], [], [stale], [stale], [stale]]
    assert expected['notes/v-leap'] == [[stale]] * len(nows)
    assert expected['notes/v-machine'][0] == [['trust', 'Trust: machine-confirmed', False],
                                              ['lifecycle', 'Lifecycle: deprecated', True]]
    assert expected['notes/v-machine'][1][-1] == stale
    draft = ['lifecycle', 'Lifecycle: draft', True]
    assert expected['notes/v-far'][3:] == [[draft], [draft, stale]]
    assert expected['notes/v-longstatus'][0] == [
        ['lifecycle', 'Lifecycle: ' + 'b' * 39 + '\U0001F600… (unrecognized)', True]]
    assert expected['notes/review'][0] == [['trust', 'Trust: human-reviewed', False], draft]
    assert all(expected[f'notes/v-{name}'] == [[]] * len(nows) for name in ('plain', 'badverify'))
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            page = browser.new_page()
            blank = tmp_path / 'blank.html'; blank.write_text('<!doctype html><title>Advisory parity</title>')
            page.goto(blank.as_uri())
            page.add_script_tag(path=str(Path(__file__).parents[1] / 'scripts/okf_loom/viewer/static/portable-retrieval.js'))
            actual = page.evaluate('''async ({corpus, nows}) => {
              const p = window.okfPortableRetrieval;
              await p.validate(corpus.manifest, corpus.chunks);
              const out = {};
              corpus.manifest.concepts.forEach(c => {
                out[c.id] = nows.map(now => p.advisoryTokens(c.metadata, now).map(t => [t.kind, t.text, t.attention]));
              });
              return out;
            }''', {'corpus': corpus, 'nows': [round(now.timestamp() * 1000) for now in nows]})
        finally:
            browser.close()
    assert actual == expected


@pytest.mark.parametrize('mode', ['live', 'static', 'single-file'])
@pytest.mark.parametrize('width', [1280, 390])
def test_search_metadata_hierarchy_in_every_viewer(tmp_path, mode, width):
    """U4: each search result keeps one muted meta line; only non-default
    advisory states join it, in words, outside the heading, wrapping without
    horizontal scroll. Driven through each viewer's real search controls."""
    from datetime import datetime, timezone
    fm, _, path = write_fixture(tmp_path / 'bundle')
    # No rich-renderer fences: this proof is about search, not CDN renderers.
    path.write_text('---\n' + yaml.safe_dump(fm, sort_keys=False) + '---\nLedger findings with a claim[^alpha].\n')
    (path.parent / 'plain.md').write_text(
        '---\ntype: Note\ntitle: Plain ledger note\n---\nThe ledger note records no advisory metadata.\n')
    evidence = Path(os.environ.get('OKF02_EVIDENCE', '/tmp/okf02-ui-evidence'))
    evidence.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        if mode == 'live':
            base = stack.enter_context(live_server(path.parents[1]))
            url = base + '/__search'
        else:
            out = tmp_path / 'site'
            build_site(Bundle.load(path.parents[1]), out, target=mode)
            if mode == 'static':
                port = free_port(); base = f'http://127.0.0.1:{port}'
                stack.enter_context(managed_server([sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1', '--directory', str(out)], base, tmp_path / 'http.log'))
                url = base + '/__search.html'
            else:
                url = (out / 'index.html').as_uri()
        pw = stack.enter_context(sync_playwright())
        browser = launch_chromium(pw); stack.callback(browser.close)
        context = browser.new_context(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
        if mode == 'static':
            # A minute before the fixture's 2026-09-08T12:00:00Z expiry.
            page.clock.install(time=datetime(2026, 9, 8, 11, 59, tzinfo=timezone.utc))
        page.goto(url)
        if mode == 'single-file':
            page.wait_for_function("() => document.body.dataset.okfSearchCaptureReady === 'ready'"
                                   " && !document.getElementById('okf-single-search-toggle').disabled")
            page.get_by_role('navigation', name='Primary navigation').get_by_role('button', name='Search', exact=True).click()
            scope = page.get_by_role('region', name='Search & context')
            form = scope.get_by_role('search')
            field_label = 'Search this bundle'
        else:
            scope = page.locator('body')
            form = page.get_by_role('search', name='Search this bundle', exact=True)
            # The workbench field is named apart from the shell's search field.
            field_label = 'Search terms'

        def search():
            form.get_by_label(field_label, exact=True).fill('ledger')
            form.get_by_role('button', name='Search', exact=True).click()

        results = scope.locator('.okf-search-result')
        review = results.filter(has=page.locator('h3', has_text='Evidence review'))
        plain = results.filter(has=page.locator('h3', has_text='Plain ledger note'))
        review_meta = review.locator('.okf-search-result__meta')
        tokens = review_meta.locator('[data-okf-advisory]')
        if mode == 'static':
            search()
            expect(tokens).to_have_text(['Trust: human-reviewed', 'Lifecycle: draft'])
            page.clock.fast_forward('01:00')
        search()
        expect(tokens).to_have_text(['Trust: human-reviewed', 'Lifecycle: draft', 'Stale'])
        expect(review_meta).to_contain_text('Advisory · Trust: human-reviewed · Lifecycle: draft · Stale')
        expect(review_meta.locator('[data-okf-advisory-attention]')).to_have_text(['Lifecycle: draft', 'Stale'])
        expect(review.locator('h3')).not_to_contain_text('Advisory')
        expect(plain.locator('.okf-search-result__meta')).to_be_visible()
        expect(plain.locator('.okf-search-advisory')).to_have_count(0)
        expect(plain.locator('.okf-search-result__meta')).not_to_contain_text('Advisory')
        expect(results).to_have_count(2)
        expect(scope.locator('.okf-search-result [data-okf-stale-at]')).to_have_count(0)
        # Defaults (unverified, stable, no expiry) never appear as tokens.
        expect(results.filter(has_text='Trust: unverified')).to_have_count(0)
        expect(results.filter(has_text='Lifecycle: stable')).to_have_count(0)
        style = review_meta.evaluate('''meta => {
          const group = meta.querySelector('.okf-search-advisory');
          const weight = node => Number(getComputedStyle(node).fontWeight);
          return {display: getComputedStyle(group).display, lineColor: getComputedStyle(meta).color,
                  groupColor: getComputedStyle(group).color,
                  trust: weight(meta.querySelector('[data-okf-advisory="trust"]')),
                  attention: [...meta.querySelectorAll('[data-okf-advisory-attention]')].map(weight)};
        }''')
        # One muted line: the group is inline and inherits the muted colour;
        # attention tokens are heavier words, not a colour-only signal.
        assert style['display'] == 'inline' and style['groupColor'] == style['lineColor'], style
        assert style['trust'] < 600 and all(weight >= 600 for weight in style['attention']), style
        widths = [width] + ([320] if width == 390 else [])
        for current in widths:
            page.set_viewport_size({'width': current, 'height': 900})
            review.scroll_into_view_if_needed()
            page.mouse.move(0, 0)  # keep link hover previews out of the evidence
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), current
            assert review_meta.evaluate('meta => meta.scrollWidth <= meta.clientWidth + 1'), current
            page.screenshot(path=str(evidence / f'search-advisory-{mode}-{current}.png'))
        assert not errors, errors


def test_metadata_clock_and_nojs_static(tmp_path):
    from datetime import datetime, timezone
    fm, _, path = write_fixture(tmp_path / 'bundle')
    # Keep this clock/no-JS proof independent of optional rich renderers.
    path.write_text('---\n' + yaml.safe_dump(fm, sort_keys=False) + '---\nClaim[^alpha].\n')
    out = tmp_path / 'site'
    build_site(Bundle.load(path.parents[1]), out, target='static')
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            page = browser.new_page(viewport={'width':320, 'height':900})
            page.clock.install(time=datetime(2026,9,8,11,59,59,tzinfo=timezone.utc))
            page.goto((out / 'notes/review.html').as_uri())
            expect(page.locator('[data-okf-freshness]')).to_have_text('Not stale')
            page.clock.fast_forward(1000)
            expect(page.locator('[data-okf-freshness]')).to_have_text('Stale')
            nojs = browser.new_context(java_script_enabled=False, viewport={'width':320,'height':900})
            plain = nojs.new_page(); plain.goto((out / 'notes/review.html').as_uri())
            metadata = plain.get_by_role('region', name='Trust, lifecycle and sources')
            expect(metadata).to_contain_text('human-reviewed')
            metadata.locator('summary').focus(); plain.keyboard.press('Enter')
            expect(metadata.get_by_role('link', name='Local evidence')).to_be_visible()
            expect(metadata).to_contain_text('Metadata only — never executed')
            assert plain.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            evidence = Path(os.environ.get('OKF02_EVIDENCE', '/tmp/okf02-ui-evidence'))
            evidence.mkdir(parents=True, exist_ok=True)
            plain.screenshot(path=str(evidence / 'static-nojs-320.png'))
        finally:
            browser.close()


def test_plain_source_footnote_hash_stays_document_local(tmp_path):
    """A source-footnote link is an ordinary document-local hash: clicking it
    (native hash navigation), revisiting it via Back/Forward, and deep-linking
    to it must keep the URL hash intact and focus the footnote target — a
    non-heading target is never treated as a section intent nor stripped."""
    _, _, path = write_fixture(tmp_path / 'bundle')
    with live_server(path.parents[1]) as base:
        url = base + '/notes/review'
        pw = sync_playwright()
        with pw as p:
            browser = launch_chromium(p)
            try:
                context = browser.new_context(reduced_motion='reduce')
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(url)
                ref = page.get_by_role('link', name='Source alpha', exact=True)
                target = ref.get_attribute('href')
                assert target and target.startswith('#')
                ref.click()
                expect(page.locator(target)).to_be_focused()
                assert page.url.endswith(target)
                # Back returns to the hashless entry natively; Forward
                # restores the hash and the document-local focus.
                page.go_back()
                assert not page.url.endswith(target)
                page.go_forward()
                expect(page.locator(target)).to_be_focused()
                assert page.url.endswith(target)
                # A direct deep link to the non-heading footnote target is
                # preserved too (old document-local behavior, no strip).
                page.goto(url + target)
                expect(page.locator(target)).to_be_focused()
                assert page.url.endswith(target)
                assert not errors, errors
            finally:
                browser.close()


@pytest.mark.parametrize('mode', ['live', 'static'])
def test_source_derivation_in_real_graph(tmp_path, mode):
    _, _, path = write_fixture(tmp_path / 'bundle')
    with ExitStack() as stack:
        if mode == 'live':
            base = stack.enter_context(live_server(path.parents[1]))
            url = base + '/__graph'
        else:
            out = tmp_path / 'site'
            build_site(Bundle.load(path.parents[1]), out, target='static')
            port = free_port(); base = f'http://127.0.0.1:{port}'
            stack.enter_context(managed_server([sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1', '--directory', str(out)], base, tmp_path / 'http.log'))
            url = base + '/__graph.html'
        pw = stack.enter_context(sync_playwright())
        browser = launch_chromium(pw); stack.callback(browser.close)
        page = browser.new_page(viewport={'width':1280,'height':900}, reduced_motion='reduce')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        page.wait_for_function('() => !!window.okfActivateGraphConcept')
        assert page.evaluate('() => window.cytoscape.version') == '3.34.3'
        page.evaluate('() => window.okfActivateGraphConcept("notes/review", "keyboard")')
        metadata = page.get_by_role('region', name='Trust, lifecycle and sources')
        expect(metadata).to_contain_text('human-reviewed')
        metadata.locator('summary').click()
        link = metadata.get_by_role('link', name='Local evidence', exact=True)
        assert link.evaluate('(el) => el.href') == base + ('/notes/source' if mode == 'live' else '/notes/source.html')
        relations = page.evaluate('() => window.__okfLoomGraph.cy.edges()[0].data("relations")')
        assert relations[0]['type'] == 'derived_from'
        assert relations[0]['origin'] == 'sources'
        assert relations[0]['source_id'] == 'alpha'
        # Compatibility proof for the unchanged dagre adapter with new Cytoscape.
        page.evaluate('() => window.__okfLoomGraph.cy.layout({name:"dagre",animate:false}).run()')
        assert page.evaluate('() => window.__okfLoomGraph.cy.nodes().every(n => Number.isFinite(n.position("x")))')
        assert not errors, errors
        link.focus(); link.scroll_into_view_if_needed()
        evidence = Path(os.environ.get('OKF02_EVIDENCE', '/tmp/okf02-ui-evidence'))
        evidence.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(evidence / f'{mode}-graph-1280.png'))
        link.click()
        expect(page.get_by_role('heading', name='Local evidence', exact=True).first).to_be_visible()
