"""Bounded live transport proofs without a many-file/YAML scale fixture."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from okf_loom import Bundle, Concept, render
from okf_loom import graph_response as transport
from okf_loom.retrieval import MAX_RESPONSE_BYTES, canonical_json_bytes
from test_graph_cache import _get, graph_server  # noqa: F401 -- shared live fixture


def generated_bundle(root: Path, count: int, *, fanout: int = 3) -> Bundle:
    concepts = {}
    for i in range(count):
        cid = f"node-{i:05d}"
        body = "# Detail\n\n" + " ".join(
            f"[Link](/node-{(i + offset + 1) % count:05d}.md)" for offset in range(fanout))
        raw = f"---\ntype: note\ntitle: Node {i}\n---\n{body}"
        concepts[(cid,)] = Concept(
            id=(cid,), path=root / (cid + '.md'), rel_path=Path(cid + '.md'),
            frontmatter={"type": "note", "title": f"Node {i}"}, body=body, raw_text=raw,
        )
    return Bundle(root, concepts=concepts)


def test_small_full_view_matches_4125166_bytes(tmp_path):
    # Golden canonical bytes from git archive 4125166 and this exact factory.
    bundle = generated_bundle(tmp_path, 10)
    body, _ = transport.graph_response(bundle, name='Compatibility fixture',
                                       comment_summary=None, structure=False,
                                       limit=MAX_RESPONSE_BYTES)
    assert len(body) == 40572
    assert hashlib.sha256(body).hexdigest() == '420e6a3fd8b24bbf77920001888fe96801cdb90d08132d6a64ee49f214cba0cf'


def test_5000_concept_root_is_bounded(tmp_path, monkeypatch):
    from test_server_smoke import _start_server
    bundle = generated_bundle(tmp_path, 5000)
    monkeypatch.setattr(render, 'build_graph_data', lambda *a, **k: pytest.fail('built graph payload'))
    server, thread = _start_server(bundle, port=0)
    try:
        status, _, body = _get(f'http://127.0.0.1:{server.server_port}', '/')
        assert status == 200
        from okf_loom.server import MAX_INDEX_RESPONSE_BYTES
        assert MAX_RESPONSE_BYTES < len(body) < MAX_INDEX_RESPONSE_BYTES
        assert b'5000 concepts' in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_root_html_allowance_remains_bounded(graph_server, monkeypatch):
    _, base = graph_server
    from okf_loom import server as http
    monkeypatch.setattr(http, '_render_index_page', lambda *a, **k: 'x' * (http.MAX_INDEX_RESPONSE_BYTES + 1))
    assert _get(base, '/')[0] == 413


def test_repeat_graph_reads_do_no_build_or_encoding(graph_server, monkeypatch):
    _, base = graph_server
    paths = ('/__data/graph.json', '/__data/graph.json?view=structure')
    first = {path: _get(base, path) for path in paths}

    def forbidden(*args, **kwargs):
        pytest.fail('repeat request rebuilt or encoded graph data')

    monkeypatch.setattr(render, 'build_graph_data', forbidden)
    monkeypatch.setattr(transport, 'canonical_json_bytes', forbidden)
    for path in paths:
        assert _get(base, path)[2] == first[path][2]
        assert _get(base, path, first[path][1]['ETag'])[0] == 304


@pytest.mark.parametrize('count', [1, 10, 100])
def test_preflight_is_a_lower_bound_for_both_views(tmp_path, count):
    bundle = generated_bundle(tmp_path, count)
    full = render.build_graph_data(bundle)
    full_size = len(canonical_json_bytes(full))
    del full['bodies']
    del full['documents']
    slim_size = len(canonical_json_bytes(full))
    assert transport.graph_size_floor(bundle) < slim_size < full_size
    assert transport._json_size_floor(full) <= slim_size


def test_5000_concept_refusal_precedes_build_and_encoding(tmp_path, monkeypatch):
    bundle = generated_bundle(tmp_path, 5000)
    assert transport.graph_size_floor(bundle) > MAX_RESPONSE_BYTES

    def forbidden(*args, **kwargs):
        pytest.fail('known oversized response built or encoded')

    monkeypatch.setattr(render, 'build_graph_data', forbidden)
    monkeypatch.setattr(transport, 'canonical_json_bytes', forbidden)
    for structure in (False, True, False, True):
        assert transport.graph_response(bundle, name='Large', comment_summary=None,
                                        structure=structure, limit=MAX_RESPONSE_BYTES) is None
    assert len(bundle._graph_response_cache) == 2


def test_size_refusal_has_actionable_json_and_is_cached(graph_server, monkeypatch):
    server, base = graph_server
    import okf_loom.retrieval as retrieval
    monkeypatch.setattr(retrieval, 'MAX_RESPONSE_BYTES', 2048)
    # The first exact-size check may render; later requests must not repeat it.
    first = _get(base)
    assert first[0] == 413
    assert first[1]['Cache-Control'] == 'no-store'
    error = json.loads(first[2])['error']
    assert error['code'] == 'response_too_large'
    assert 'smaller bundle' in error['message']
    assert str(server.bundle.root) not in error['message']
    monkeypatch.setattr(render, 'build_graph_data', lambda *a, **k: pytest.fail('rebuilt refusal'))
    assert _get(base)[2] == first[2]


def test_cached_structure_refusal_skips_full_body_render(graph_server, monkeypatch):
    server, base = graph_server
    import okf_loom.retrieval as retrieval
    monkeypatch.setattr(retrieval, 'MAX_RESPONSE_BYTES', 1500)
    assert _get(base, '/__data/graph.json?view=structure')[0] == 413
    monkeypatch.setattr(render, '_render_graph_documents', lambda *a, **k: pytest.fail('rendered bodies'))
    assert _get(base)[0] == 413
    assert not hasattr(server.bundle, '_graph_documents_cache')


def test_live_thumbnail_minimal_data_preserves_exact_html(tmp_path):
    bundle = generated_bundle(tmp_path, 40)
    minimal = render._graph_thumbnail_data(bundle)
    assert render._graph_thumbnail_html(minimal, '/__graph') == render._graph_thumbnail_html(
        render.build_graph_data(bundle), '/__graph')
    assert all(set(node['data']) == {'id'} for node in minimal['nodes'])


def test_root_does_not_build_full_graph_payload(graph_server, monkeypatch):
    _, base = graph_server
    monkeypatch.setattr(render, 'build_graph_data', lambda *a, **k: pytest.fail('built graph payload'))
    assert _get(base, '/')[0] == 200


def test_failed_transport_build_does_not_cache_failure(graph_server, monkeypatch):
    server, _ = graph_server
    original = render.build_graph_data
    def failed(*args, **kwargs):
        raise ValueError('failed build')
    monkeypatch.setattr(render, 'build_graph_data', failed)
    with pytest.raises(ValueError, match='failed build'):
        transport.graph_response(server.bundle, name='Graph', comment_summary=None,
                                 structure=False, limit=MAX_RESPONSE_BYTES)
    monkeypatch.setattr(render, 'build_graph_data', original)
    assert transport.graph_response(server.bundle, name='Graph', comment_summary=None,
                                    structure=False, limit=MAX_RESPONSE_BYTES) is not None
