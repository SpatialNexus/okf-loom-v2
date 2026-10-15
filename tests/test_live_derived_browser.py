"""Cross-tab derived reading views: real watcher/CLI delivery plus async fences."""
from contextlib import contextmanager
from pathlib import Path
import os
import subprocess

import pytest

from browser_support import copy_bundle, expect, live_server
from okf_loom import Bundle
from okf_loom.server import (
    _BundleWatcher, _start_public_ingress, _stop_public_ingress,
    _shutdown_retrieval_runtime,
)
from test_studio_iter1_browser import page, _wait_for_studio
from test_tunnel import _app_server

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.browser
TARGET = 'tables/users'
CARD = f'.okf-card[data-concept-id="{TARGET}"]'
CONNECTION = f'.okf-conn[data-concept-id="{TARGET}"] .okf-conn__title'


@pytest.fixture
def bundle(tmp_path):
    root = copy_bundle(ROOT / 'tests/fixtures/tiny_good', tmp_path / 'bundle')
    # Make scroll preservation meaningful on both reading surfaces.
    for n in range(12):
        (root / f'filler-{n}.md').write_text(
            f'---\ntype: Note\ntitle: Filler {n}\n---\nA stable concept.\n')
    for document in ['tables/events.md', 'tables/users.md', 'references/orphan.md']:
        with (root / document).open('a') as stream:
            stream.write('\n\n' + '\n\n'.join(f'Paragraph {n}. ' * 30 for n in range(25)))
    return root


def cli(bundle, *args):
    result = subprocess.run(
        [str(ROOT / 'scripts/okf-loom'), *args, '--bundle', str(bundle)],
        env={**os.environ, 'PYTHON': os.sys.executable},
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def ready(tab, base, route, *, scroll=True):
    tab.goto(base + route, wait_until='load')
    _wait_for_studio(tab)
    tab.wait_for_function('''() => window.okfLoomLive?.state === 'online' &&
        window.okfLoomStudio.state.commentsReady && window.okfLoomStudio.state.eventsReady''')
    # Initial connection reconciliation must settle before measuring a burst.
    tab.evaluate('''() => new Promise(resolve => {
        window.addEventListener('okf-loom:derivedPatched', resolve, {once: true});
        window.okfLoomLive.emit('graph', {});
    })''')
    tab.evaluate('''() => { window.survivesRefresh = {}; window.scrollTo(0, 280); }''')
    if scroll:
        assert tab.evaluate('scrollY') == 280


def assert_preserved(tab):
    assert tab.evaluate('!!window.survivesRefresh')
    assert tab.evaluate('scrollY') == 280
    assert tab.evaluate('document.documentElement.classList.contains("okf-theme-ready")')


def assert_target(index, reader, title, description):
    expect(index.locator(CARD + ' .okf-card__link')).to_have_text(title, timeout=20000)
    expect(index.locator(CARD + ' .okf-card__desc')).to_have_text(description)
    expect(reader.locator(CONNECTION)).to_have_text([title, title], timeout=20000)
    expect(index.locator(CARD + ' .okf-card__type-label')).to_have_text('Revised')
    expect(index.locator(CARD)).to_have_attribute('data-index-instant', '2026-10-15T12:00:00+00:00')
    expect(index.locator('.okf-stat').nth(1)).to_have_text('4 types')
    expect(index.locator('[data-index-section][data-index-type="Revised"] .okf-section__count')).to_have_text('1')


@pytest.mark.parametrize('method', ['watcher', 'cli'])
def test_other_tabs_refresh_cards_stats_connections_and_backlinks(bundle, page, method):
    with live_server(bundle) as base:
        index = page
        reader = page.context.new_page()
        target = page.context.new_page()
        ready(index, base, '/')
        ready(reader, base, '/tables/events')
        ready(target, base, '/' + TARGET)
        # Retain real controls, selection, and a live comment draft in tab B.
        index.locator('[data-index-order]').select_option('recent')
        index.evaluate('''() => {
            window.cardLink = document.querySelector('.okf-card[data-concept-id="tables/users"] .okf-card__link');
            cardLink.focus({preventScroll:true});
            const text = document.querySelector('.okf-card[data-concept-id="filler-0"] .okf-card__link').firstChild;
            getSelection().setBaseAndExtent(text, 0, text, 6);
            scrollTo(0, 280);
        }''')
        reader.locator('.okf-studio-launcher').click()
        reader.locator('#okf-panel').get_by_role('button', name='Comments', exact=True).click()
        draft = reader.locator('.okf-composer__textarea')
        draft.fill('Keep this unfinished comment')
        reader.evaluate('''() => {
            window.draftInput = document.activeElement;
            document.querySelector('#okf-document-details').open = true;
            scrollTo(0, 280);
        }''')
        title, description = 'Updated users', 'An updated users description.'
        values = {'title': title, 'description': description, 'type': 'Revised', 'timestamp': '2026-10-15T12:00:00Z'}
        if method == 'watcher':
            source = bundle / (TARGET + '.md')
            raw = source.read_text().replace('title: Users', 'title: ' + title).replace(
                'description: The users table.', 'description: ' + description).replace(
                'type: BigQuery Table', 'type: Revised').replace(
                "timestamp: '2026-01-15T10:00:00Z'", "timestamp: '2026-10-15T12:00:00Z'")
            source.write_text(raw)
        else:
            for key, value in values.items():
                cli(bundle, 'set-frontmatter', '--id', TARGET, '--key', key, '--value', value)
        assert_target(index, reader, title, description)
        expect(target.locator('.okf-page__title')).to_have_text(title, timeout=20000)
        assert index.evaluate('window.cardLink === document.activeElement')
        assert index.evaluate('getSelection().toString()') == 'Filler'
        expect(index.locator('[data-index-order]')).to_have_value('recent')
        expect(draft).to_have_value('Keep this unfinished comment')
        assert reader.evaluate('window.draftInput === document.activeElement')
        assert reader.locator('#okf-document-details').evaluate('node => node.open')
        for tab in [index, reader, target]:
            assert_preserved(tab)
        links = int(index.locator('.okf-stat strong').nth(2).inner_text())
        cli(bundle, 'link-add', '--source', 'references/orphan', '--target', TARGET)
        expect(target.locator('#okf-backlinks .okf-conn[data-concept-id="references/orphan"]')).to_have_count(1, timeout=20000)
        expect(index.locator('.okf-stat').nth(2)).to_have_text(f'{links + 1} links', timeout=20000)
        for tab in [index, reader, target]:
            assert_preserved(tab)
        result = index.evaluate('''async () => await window.okfCaptureReadiness.start({target:'document'}).promise''')
        assert result['screenshotAllowed'], result


@contextmanager
def public_watcher_server(bundle):
    """Use the real generation-bound public ingress and the existing tunnel harness."""
    server = _app_server(bundle)
    ingress = _start_public_ingress(server, allow_public_edit=False)

    def reload():
        with server._state_lock:
            server.bundle = Bundle.load(bundle)
        server.studio.emit_change(kind='changed', ids=[TARGET, 'references/orphan'], origin='disk')

    watcher = _BundleWatcher(bundle, reload, interval=0.1)
    watcher.start()
    try:
        yield f'http://127.0.0.1:{ingress.server_address[1]}'
    finally:
        watcher.stop()
        server.studio.bus.close()
        _stop_public_ingress(server)
        _shutdown_retrieval_runtime(server)
        server.server_close()


@pytest.mark.parametrize('authority', ['public', 'no-edit'])
def test_readonly_viewers_refresh_without_acquiring_more_authority(bundle, page, authority):
    server = public_watcher_server(bundle) if authority == 'public' else live_server(bundle, '--no-edit')
    # A pruned input must never enter a refreshed inventory or connection list.
    (bundle / '.private').mkdir()
    (bundle / '.private/secret.md').write_text('---\ntype: Secret\ntitle: Hidden secret\n---\nPrivate\n')
    with server as base:
        reader = page.context.new_page()
        target = page.context.new_page()
        ready(page, base, '/')
        ready(reader, base, '/tables/events')
        ready(target, base, '/' + TARGET)
        source = bundle / (TARGET + '.md')
        source.write_text(source.read_text().replace('title: Users', 'title: Readonly update').replace(
            'description: The users table.', 'description: Shared updated description.').replace(
            'type: BigQuery Table', 'type: Revised').replace(
            "timestamp: '2026-01-15T10:00:00Z'", "timestamp: '2026-10-15T12:00:00Z'"))
        assert_target(page, reader, 'Readonly update', 'Shared updated description.')
        links = int(page.locator('.okf-stat strong').nth(2).inner_text())
        with (bundle / 'references/orphan.md').open('a') as stream:
            stream.write('\n[Users](/tables/users.md)\n')
        expect(target.locator('#okf-backlinks .okf-conn[data-concept-id="references/orphan"]')).to_have_count(1, timeout=20000)
        expect(page.locator('.okf-stat').nth(2)).to_have_text(f'{links + 1} links', timeout=20000)
        for tab in [page, reader, target]:
            assert_preserved(tab)
            assert not tab.evaluate('window.__OKF_LOOM_STUDIO__.edit')
            assert tab.locator('.okf-composer textarea').count() == 0
            assert 'Hidden secret' not in tab.locator('body').inner_text()
        if authority == 'public':
            assert not page.evaluate('window.__OKF_LOOM_STUDIO__.token')
            for route in ['/__presence', '/__diff?concept=tables/users&from=x&to=y']:
                assert page.request.get(base + route).status == 403
        assert page.request.post(base + '/__apply', data={'kind': 'add_tag', 'target': TARGET, 'args': {'tag': 'forbidden'}}).status == 403


def test_created_removed_inventory_filters_and_first_connections(bundle, page):
    with live_server(bundle) as base:
        directory = page.context.new_page()
        orphan = page.context.new_page()
        ready(page, base, '/')
        ready(directory, base, '/tables/', scroll=False)
        ready(orphan, base, '/references/orphan')
        concepts = int(page.locator('.okf-stat strong').first.inner_text())
        page.locator('[data-index-order]').select_option('recent')
        dated = page.locator('[data-index-section][data-index-type="BigQuery Table"] .okf-card__link')
        expect(dated).to_have_text(['Events', 'Users'])
        cli(bundle, 'set-frontmatter', '--id', TARGET, '--key', 'timestamp', '--value', '2026-10-15T12:00:00Z')
        expect(dated).to_have_text(['Users', 'Events'], timeout=20000)
        page.locator('select[data-index-type]').select_option('Note')
        page.locator('[data-index-order]').select_option('title')
        source = bundle / 'tables/new.md'
        source.write_text('---\ntype: New type\ntitle: A new concept\n---\n[Orphan](/references/orphan.md)\n')
        expect(page.locator('.okf-stat').first).to_have_text(f'{concepts + 1} concepts', timeout=20000)
        expect(directory.locator('.okf-card[data-concept-id="tables/new"] .okf-card__link')).to_have_text('A new concept', timeout=20000)
        expect(orphan.locator('#okf-backlinks .okf-conn[data-concept-id="tables/new"]')).to_have_count(1, timeout=20000)
        # Existing filter/order remain selected. A newly-created type chip uses
        # the existing delegated jump/filter owner rather than a dead listener.
        expect(page.locator('select[data-index-type]')).to_have_value('Note')
        expect(page.locator('[data-index-order]')).to_have_value('title')
        page.locator('.okf-type-nav__link[data-index-type="New type"]').click()
        expect(page.locator('select[data-index-type]')).to_have_value('New type')
        expect(page.locator('.okf-card[data-concept-id="tables/new"]')).to_be_visible()
        source.unlink()
        expect(page.locator('.okf-card[data-concept-id="tables/new"]')).to_have_count(0, timeout=20000)
        expect(directory.locator('.okf-card[data-concept-id="tables/new"]')).to_have_count(0, timeout=20000)
        expect(page.locator('.okf-stat').first).to_have_text(f'{concepts} concepts')
        expect(page.locator('select[data-index-type]')).to_have_value('')
        expect(page.locator('[data-index-status]')).to_have_text(f'{concepts} concepts shown.')
        expect(orphan.locator('.okf-page__connections')).to_have_count(0, timeout=20000)
        assert all(tab.evaluate('!!window.survivesRefresh') for tab in [page, directory, orphan])


def test_connections_disclosure_threshold_preserves_links_focus_and_open_state(bundle, page):
    with live_server(bundle) as base:
        ready(page, base, '/' + TARGET)
        page.evaluate('''() => {
            window.connectionLink = document.querySelector('#okf-outgoing .okf-conn__title');
            connectionLink.focus({preventScroll:true});
        }''')
        for n in range(10):
            with (bundle / f'filler-{n}.md').open('a') as stream:
                stream.write('\n[Users](/tables/users.md)\n')
        expect(page.locator('.okf-connections-disclosure')).to_have_count(1, timeout=20000)
        assert page.locator('.okf-connections-disclosure').evaluate('node => node.open')
        assert page.evaluate('document.activeElement === window.connectionLink')
        # A second refresh does not collapse an already-open disclosure.
        source = bundle / 'tables/events.md'
        source.write_text(source.read_text().replace('title: Events', 'title: Renamed events'))
        expect(page.locator('#okf-outgoing .okf-conn[data-concept-id="tables/events"] .okf-conn__title')).to_have_text('Renamed events', timeout=20000)
        assert page.locator('.okf-connections-disclosure').evaluate('node => node.open')
        for n in range(10):
            source = bundle / f'filler-{n}.md'
            source.write_text(source.read_text().replace('\n[Users](/tables/users.md)\n', ''))
        expect(page.locator('.okf-connections-disclosure')).to_have_count(0, timeout=20000)
        assert page.evaluate('document.activeElement === window.connectionLink')
        assert_preserved(page)


def test_derived_refresh_coalesces_and_fences_stale_and_suspended_reads(bundle, page):
    with live_server(bundle) as base:
        ready(page, base, '/')
        page.evaluate('''() => {
            const native = window.fetch.bind(window);
            window.reads = [];
            window.fetch = (url, options) => {
                if (window.delayRestoreComments && String(url).startsWith('/__comments')) {
                    return native(url, options).then(response => new Promise(resolve => setTimeout(() => resolve(response), 700)));
                }
                if (url !== '/') return native(url, options);
                const read = {aborted: () => options.signal.aborted};
                reads.push(read);
                read.title = 'Held ' + reads.length;
                // Ignore abort deliberately: only admission fences prevent a
                // response body already buffered by the browser from committing.
                return native(url).then(async response => {
                    const html = (await response.text()).replaceAll('Users', read.title);
                    return new Promise(resolve => { read.release = (status=200) => resolve(new Response(html, {status})); });
                });
            };
            window.burst = () => { for (let n=0; n<20; n++) window.okfLoomLive.emit('graph', {}); };
            burst();
        }''')
        page.wait_for_function('() => reads.length === 1 && reads[0].release')
        page.wait_for_timeout(350)
        assert page.evaluate('reads.length') == 1
        page.evaluate('burst()')
        assert page.evaluate('reads[0].aborted()')
        page.wait_for_function('() => reads.length === 2 && reads[1].release')
        page.evaluate('reads[1].release()')
        expect(page.locator(CARD + ' .okf-card__link')).to_have_text('Held 2')
        page.evaluate('reads[0].release()')
        page.wait_for_timeout(100)
        expect(page.locator(CARD + ' .okf-card__link')).to_have_text('Held 2')
        page.evaluate('burst()')
        page.wait_for_function('() => reads.length === 3 && reads[2].release')
        page.evaluate('''() => {
            dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
            reads[2].release();
            dispatchEvent(new PageTransitionEvent('pageshow', {persisted:false}));
        }''')
        page.wait_for_timeout(350)
        expect(page.locator(CARD + ' .okf-card__link')).to_have_text('Held 2')
        assert page.evaluate('reads.length') == 3
        page.evaluate('''() => {
            window.restoreReady = false;
            window.delayRestoreComments = true;
            window.EventSource = class extends window.EventSource {
                constructor(url) {
                    super(url);
                    this.addEventListener('ready', () => { window.restoreReady = true; });
                }
            };
            dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
        }''')
        # Restore resync and the fresh transport's ready can be >250 ms apart
        # on a loaded host. Ready correctly supersedes an earlier resync read;
        # release only the read owned by that completed handoff, not ordinal 4.
        page.wait_for_function('() => restoreReady')
        page.evaluate('window.delayRestoreComments = false')
        page.wait_for_function('() => reads.length >= 4 && reads.at(-1).release && !reads.at(-1).aborted()')
        restored = page.evaluate('reads.length')
        page.evaluate('reads.at(-1).release()')
        expect(page.locator(CARD + ' .okf-card__link')).to_have_text(f'Held {restored}')
        page.evaluate('burst()')
        page.wait_for_function('(count) => reads.length === count && reads.at(-1).release', arg=restored + 1)
        page.evaluate('reads.at(-1).release(503)')
        page.wait_for_timeout(100)
        expect(page.locator(CARD + ' .okf-card__link')).to_have_text(f'Held {restored}')
        page.evaluate('burst()')
        page.wait_for_function('(count) => reads.length === count && reads.at(-1).release', arg=restored + 2)
        page.evaluate('''() => {
            dispatchEvent(new PopStateEvent('popstate'));
            reads.at(-1).release();
        }''')
        page.wait_for_timeout(100)
        expect(page.locator(CARD + ' .okf-card__link')).to_have_text(f'Held {restored}')
        page.evaluate('burst()')
        page.wait_for_function('(count) => reads.length === count && reads.at(-1).release', arg=restored + 3)
        page.evaluate('reads.at(-1).release()')
        expect(page.locator(CARD + ' .okf-card__link')).to_have_text(f'Held {restored + 3}')
        assert_preserved(page)
