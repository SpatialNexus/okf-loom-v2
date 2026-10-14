"""Live graph rendering cache, conditional reads, clock and reload proofs."""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom import render
from okf_loom.retrieval import canonical_json_bytes
from test_server_smoke import _start_server


def _get(base, path='/__data/graph.json', etag=None, *, validators=(), method='GET'):
    from urllib.parse import urlparse
    url = urlparse(base)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=20)
    conn.putrequest(method, path)
    for value in ((etag,) if etag else validators):
        conn.putheader('If-None-Match', value)
    conn.endheaders()
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


@pytest.fixture
def graph_server(tmp_path):
    (tmp_path / 'index.md').write_text('# Graph cache\n')
    (tmp_path / 'a.md').write_text(
        '---\ntype: note\ntitle: Alpha\nstale_after: "2030-01-01T00:00:00Z"\n'
        'sources:\n  - id: source-1\n    url: https://example.org\n---\n'
        '# Body\n\nOld content [^source-1].\n\n[^source-1]: Source\n'
    )
    server, thread = _start_server(Bundle.load(tmp_path), port=0)
    try:
        yield server, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('root', ['docs-bundle', 'samples/demo_bundle'])
def test_cached_graph_is_byte_identical_to_uncached(root):
    bundle = Bundle.load(root)
    expected = canonical_json_bytes(render.build_graph_data(bundle))
    assert canonical_json_bytes(render.build_graph_data(bundle, cache_bodies=True)) == expected
    assert canonical_json_bytes(render.build_graph_data(bundle, cache_bodies=True)) == expected


def test_cached_graph_documents_are_isolated_from_caller_mutation(graph_server):
    server, _ = graph_server
    first = render.build_graph_data(server.bundle, cache_bodies=True)
    expected = canonical_json_bytes(first)
    first['bodies']['a'] = '<script>tampered</script>'
    first['documents']['index.md']['html'] = 'tampered'
    first['documents']['index.md']['document'] = 'other.md'
    first['documents']['injected.md'] = {'html': 'tampered'}
    first['bodies'].clear()
    assert canonical_json_bytes(render.build_graph_data(server.bundle, cache_bodies=True)) == expected


@pytest.mark.parametrize('query, expected, structure', [
    ('?cache_buster=1', 200, False),
    ('?view=structure&cache_buster=1', 200, True),
    ('?view=unknown', 400, False),
    ('?view=', 400, False),
    ('?view=structure&view=structure', 400, False),
    ('', 200, False),
])
def test_graph_query_interprets_only_one_structure_view(graph_server, query, expected, structure):
    _, base = graph_server
    status, _, body = _get(base, '/__data/graph.json' + query)
    assert status == expected
    data = json.loads(body)
    if expected == 400:
        assert data == {'ok': False, 'error': 'invalid_request'}
    else:
        assert ('bodies' not in data and 'documents' not in data) is structure
        if not structure:
            assert body == _get(base)[2]


def test_slow_graph_render_does_not_block_revision_or_structure_reads(graph_server, monkeypatch):
    from okf_loom.retrieval import bundle_revision
    server, base = graph_server
    original = render._render_graph_documents
    entered, release = threading.Event(), threading.Event()

    def blocked(bundle):
        entered.set()
        assert release.wait(10)
        return original(bundle)

    monkeypatch.setattr(render, '_render_graph_documents', blocked)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        cold = pool.submit(_get, base)
        try:
            assert entered.wait(10)
            assert pool.submit(bundle_revision, server.bundle).result(timeout=3)
            assert pool.submit(_get, base, '/__data/graph.json?view=structure').result(timeout=3)[0] == 200
        finally:
            release.set()
        assert cold.result()[0] == 200


def test_stale_graph_flight_cannot_publish_over_new_generation(graph_server, monkeypatch):
    server, _ = graph_server
    original = render._render_graph_documents
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def blocked(bundle):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(10)
        return original(bundle)

    monkeypatch.setattr(render, '_render_graph_documents', blocked)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        cold = pool.submit(render._graph_documents, server.bundle, cached=True)
        try:
            assert entered.wait(10)
            source = server.bundle.root / 'a.md'
            source.write_text(source.read_text().replace('Old content', 'New content'))
            server.bundle.reload()
            fresh = render._graph_documents(server.bundle, cached=True)
            published = server.bundle._graph_documents_cache
            assert 'New content' in fresh[0]['a']
        finally:
            release.set()
        old = cold.result()
    assert 'Old content' in old[0]['a'] and 'New content' not in old[0]['a']
    assert server.bundle._graph_documents_cache is published
    assert render._graph_documents(server.bundle, cached=True) == fresh
    assert calls == 2 and server.bundle._graph_documents_inflight == {}


def test_failed_graph_flight_releases_waiters_and_allows_retry(graph_server, monkeypatch):
    server, _ = graph_server
    original = render._render_graph_documents
    entered, release = threading.Event(), threading.Event()
    def failed(bundle):
        entered.set()
        assert release.wait(10)
        raise ValueError('render failed')
    monkeypatch.setattr(render, '_render_graph_documents', failed)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(render._graph_documents, server.bundle, cached=True)
        try:
            assert entered.wait(10)
            with server.bundle._derived_lock:
                flight = next(iter(server.bundle._graph_documents_inflight.values()))
            waiter = pool.submit(flight.result)
        finally:
            release.set()
        for request in (owner, waiter):
            with pytest.raises(ValueError, match='render failed'):
                request.result(timeout=3)
    assert server.bundle._graph_documents_inflight == {}
    monkeypatch.setattr(render, '_render_graph_documents', original)
    assert 'Old content' in render._graph_documents(server.bundle, cached=True)[0]['a']


def test_graph_single_flight_across_requests_and_html(graph_server, monkeypatch):
    server, base = graph_server
    original = render._render_graph_documents
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked(bundle):
        calls.append(bundle)
        entered.set()
        assert release.wait(10)
        return original(bundle)

    monkeypatch.setattr(render, '_render_graph_documents', blocked)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_get, base) for _ in range(6)]
        assert entered.wait(10)
        release.set()
        results = [future.result() for future in futures]
    assert len(calls) == 1
    assert all(status == 200 for status, _, _ in results)
    assert len({body for _, _, body in results}) == 1
    assert _get(base, '/')[0] == 200
    assert _get(base, '/__graph')[0] == 200
    assert len(calls) == 1
    # Retained mutations/reload have the same one-entry, generation-owned cache.
    source = server.bundle.root / 'a.md'
    source.write_text(source.read_text().replace('Old content', 'New content'))
    server.bundle.reload()
    status, headers, body = _get(base, etag=results[0][1]['ETag'])
    assert status == 200 and b'New content' in body
    assert headers['ETag'] != results[0][1]['ETag']
    assert len(calls) == 2


@pytest.mark.parametrize('path', ['/__data/graph.json', '/__data/graph.json?view=structure'])
def test_graph_if_none_match_all_fields_and_quoted_commas(graph_server, path):
    _, base = graph_server
    _, headers, _ = _get(base, path)
    tag = headers['ETag']
    for method in ('GET', 'HEAD'):
        for fields in (('"other"', tag), ('"other,*,tag", W/' + tag,), ('*',), ('W/' + tag,)):
            status, fresh, body = _get(base, path, validators=fields, method=method)
            assert status == 304 and body == b'' and fresh['ETag'] == tag
            assert fresh['Cache-Control'] == headers['Cache-Control'] == 'private, no-cache'
        assert _get(base, path, validators=('"other,*,tag"',), method=method)[0] == 200


def test_graph_etag_tracks_clock_not_only_content_revision(graph_server, monkeypatch):
    from okf_loom.viewer import metadata
    server, base = graph_server

    class Clock(datetime):
        instant = datetime(2029, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant

    monkeypatch.setattr(metadata, 'datetime', Clock)
    status, headers, before = _get(base)
    assert status == 200 and headers['Cache-Control'] == 'private, no-cache'
    assert b'Not stale' in before
    cached = server.bundle._graph_documents_cache
    current = headers['ETag']
    assert not current.startswith('W/')
    for validator in (current, 'W/' + current, '"unrelated", ' + current, '*'):
        code, fresh_headers, body = _get(base, etag=validator)
        assert code == 304 and body == b'' and fresh_headers['ETag'] == current
    Clock.instant = datetime(2030, 1, 1, tzinfo=timezone.utc)
    status, new_headers, after = _get(base, etag=current)
    assert status == 200 and before != after and b'>Stale<' in after
    assert new_headers['ETag'] != current
    assert json.loads(before)['bundle_revision'] == json.loads(after)['bundle_revision']
    assert server.bundle._graph_documents_cache is cached
    assert _get(base, etag=new_headers['ETag'])[0] == 304


def test_graph_nonbody_inputs_and_structure_view_remain_fresh(graph_server, monkeypatch):
    from okf_loom.studio import canonical_comment_summary_revision
    server, base = graph_server
    first = _get(base)
    cached = server.bundle._graph_documents_cache
    server.name = 'Different name'
    monkeypatch.setattr(render, 'resolve_palette', lambda bundle: {'note': '#112233'})
    counts = {'a': 2}

    class Summary:
        def comment_summary(self, **kwargs):
            return {'schema': 1, 'semantics': 'active-comment-row', 'counts': counts,
                    'summary_revision': canonical_comment_summary_revision(counts)}

    server.studio = Summary()
    status, headers, body = _get(base, etag=first[1]['ETag'])
    assert status == 200 and headers['ETag'] != first[1]['ETag']
    data = json.loads(body)
    assert data['name'] == 'Different name'
    assert data['palette'] == {'note': '#112233'}
    assert data['nodes'][0]['data']['active_comment_count'] == 2
    assert server.bundle._graph_documents_cache is cached
    counts['a'] = 3
    assert _get(base, etag=headers['ETag'])[0] == 200
    status, slim_headers, slim_body = _get(base, '/__data/graph.json?view=structure')
    assert status == 200 and slim_headers['ETag'] != headers['ETag']
    slim = json.loads(slim_body)
    assert 'bodies' not in slim and 'documents' not in slim
    full = json.loads(_get(base)[2])
    assert slim == {key: value for key, value in full.items() if key not in {'bodies', 'documents'}}
    assert _get(base, '/__data/graph.json?view=structure', slim_headers['ETag'])[0] == 304
    assert _get(base, '/__data/graph.json?view=unknown')[0] == 400
    server.bundle = Bundle.load(server.bundle.root)
    _get(base)
    assert server.bundle._graph_documents_cache is not cached


@pytest.mark.parametrize('path', ['/', '/__graph', '/__data/graph.json?view=structure'])
def test_graph_html_and_structure_never_render_unused_bodies(graph_server, monkeypatch, path):
    _, base = graph_server

    def forbidden(bundle):
        pytest.fail('structure view rendered graph bodies')

    monkeypatch.setattr(render, '_render_graph_documents', forbidden)
    assert _get(base, path)[0] == 200


def test_graph_public_and_local_views_are_identical(tmp_path):
    from test_tunnel import _app_server
    from okf_loom.server import _start_public_ingress
    (tmp_path / 'a.md').write_text('---\ntype: note\n---\nPublic body\n')
    server = _app_server(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ingress = _start_public_ingress(server, allow_public_edit=False)
    try:
        local = f'http://127.0.0.1:{server.server_port}'
        public = f'http://127.0.0.1:{ingress.server_port}'
        for path in ('/__data/graph.json', '/__data/graph.json?view=structure'):
            local_response, public_response = _get(local, path), _get(public, path)
            assert local_response[0] == public_response[0] == 200
            assert local_response[2] == public_response[2]
            assert local_response[1]['ETag'] == public_response[1]['ETag']
            assert _get(public, path, local_response[1]['ETag'])[0] == 304
    finally:
        ingress.shutdown()
        ingress.server_close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        server.studio.close()


def test_graph_watcher_and_cli_mutator_invalidate_live_cache(tmp_path):
    from browser_support import live_server
    from conftest import okf_module_argv, okf_subprocess_env
    root = tmp_path / 'bundle'
    root.mkdir()
    source = root / 'a.md'
    source.write_text('---\ntype: note\ntitle: Old title\n---\nOld body\n')
    with live_server(root) as base:
        first = _get(base)
        source.write_text(source.read_text().replace('Old body', 'Watcher body'))
        deadline = time.monotonic() + 15
        while True:
            updated = _get(base, etag=first[1]['ETag'])
            if updated[0] == 200 and b'Watcher body' in updated[2]:
                break
            assert time.monotonic() < deadline, 'watcher did not publish graph change'
            time.sleep(.1)
        assert updated[1]['ETag'] != first[1]['ETag']
        assert _get(base, etag=updated[1]['ETag'])[0] == 304
        assert json.loads(updated[2])['bundle_revision'].encode() in _get(base, '/__graph')[2]
        subprocess.run(okf_module_argv('set-frontmatter', '--bundle', str(root),
                                     '--id', 'a', '--key', 'title', '--value', 'Mutator title'),
                       env=okf_subprocess_env(), check=True, capture_output=True)
        deadline = time.monotonic() + 15
        while True:
            latest = _get(base, etag=updated[1]['ETag'])
            if latest[0] == 200 and b'Mutator title' in latest[2]:
                break
            assert time.monotonic() < deadline, 'mutator did not publish graph change'
            time.sleep(.1)
        assert latest[1]['ETag'] != updated[1]['ETag']
        assert _get(base, etag=latest[1]['ETag'])[0] == 304
        page = _get(base, '/__graph')[2]
        assert b'Mutator title' in page
        assert json.loads(latest[2])['bundle_revision'].encode() in page
