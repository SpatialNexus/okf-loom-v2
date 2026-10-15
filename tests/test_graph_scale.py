"""Compact live graph contract, without parsing thousands of fixture files."""
from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest

from okf_loom import render
from okf_loom import graph_response as transport
from okf_loom.retrieval import canonical_json_bytes
from okf_loom.studio import canonical_comment_summary_revision
from test_graph_cache import _get, graph_server  # noqa: F401
from test_graph_response import generated_bundle


def test_structure_fits_documented_cap_at_5000_without_rendering_html(tmp_path, monkeypatch):
    bundle = generated_bundle(tmp_path, 5000)
    monkeypatch.setattr(render, '_render_graph_documents', lambda *a, **k: pytest.fail('rendered documents'))
    monkeypatch.setattr(render, 'render_metadata', lambda *a, **k: pytest.fail('rendered metadata'))
    summary = dict(schema=1, semantics='active-comment-row', counts={},
                   summary_revision=canonical_comment_summary_revision({}))
    body, etag = transport.graph_response(bundle, name='Scale', comment_summary=summary,
                                         structure=True, limit=transport.MAX_STRUCTURE_BYTES)
    assert len(body) < transport.MAX_STRUCTURE_BYTES == 16 * 1024 * 1024
    data = json.loads(body)
    assert len(data['nodes']) == 5000
    assert len(data['edges']) == 15000
    assert data['structure_schema'] == 1
    assert 'bodies' not in data and 'documents' not in data
    assert all(node['data']['active_comment_count'] == 0 for node in data['nodes'])
    assert all('metadata_html' not in node['data'] for node in data['nodes'])
    assert etag.startswith('"')


def test_compact_relations_preserve_every_read_field_and_primary_identity(graph_server):
    _, base = graph_server
    full = json.loads(_get(base)[2])
    slim = json.loads(_get(base, '/__data/graph.json?view=structure')[2])
    for old, new in zip(full['nodes'], slim['nodes']):
        for key, value in old['data'].items():
            if key in {'okf_metadata', 'metadata_html', 'generated_at', 'body_chars', 'color', 'graph_cluster', 'source_system'}:
                continue
            assert new['data'].get(key, value if not value else None) == value, key
    for old, new in zip(full['edges'], slim['edges']):
        old, new = old['data'], new['data']
        assert 'primary_relation' not in new and 'multiplicity' not in new
        assert new['relations'][new['primary_relation_index']]['id'] == old['primary_relation']['id']
        assert new['source'] == old['source'] and new['target'] == old['target']
        assert new['relations'] == [{key: value for key, value in relation.items()
                                    if key in {'id', 'label', 'type', 'kind', 'detail'}}
                                   for relation in old['relations']]
    assert len(canonical_json_bytes(slim)) < len(canonical_json_bytes(full))


def test_structure_floor_refuses_before_build_or_encode(tmp_path, monkeypatch):
    bundle = generated_bundle(tmp_path, 5000)
    limit = transport.graph_size_floor(bundle, structure=True) - 1
    monkeypatch.setattr(render, 'build_graph_data', lambda *a, **k: pytest.fail('built known oversize graph'))
    monkeypatch.setattr(transport, 'canonical_json_bytes', lambda *a, **k: pytest.fail('encoded known oversize graph'))
    assert transport.graph_response(bundle, name='Scale', comment_summary=None,
                                    structure=True, limit=limit) is None


def test_graph_body_is_exact_full_view_html_and_revision_bound(graph_server, monkeypatch):
    server, base = graph_server
    full = json.loads(_get(base)[2])
    query = urlencode({'id': 'a', 'revision': full['bundle_revision']})
    # No full document render, annotation projection or graph rebuilding.
    monkeypatch.setattr(render, '_render_graph_documents', lambda *a, **k: pytest.fail('rendered every body'))
    monkeypatch.setattr(server.bundle, 'graph', lambda *a, **k: pytest.fail('rebuilt graph'))
    status, headers, body = _get(base, '/__data/graph-body?' + query)
    assert status == 200
    assert headers['Cache-Control'] == 'private, no-cache'
    doc = json.loads(body)
    assert doc == {'id': 'a', 'bundle_revision': full['bundle_revision'],
                   'html': full['bodies']['a'], 'metadata_html': full['nodes'][0]['data']['metadata_html']}
    assert _get(base, '/__data/graph-body?' + urlencode({'id': 'a', 'revision': 'stale'}))[0] == 409
    assert _get(base, '/__data/graph-body?' + urlencode({'id': 'absent', 'revision': 'stale'}))[0] == 404
    assert _get(base, '/__data/graph-body?' + query + '&id=b')[0] == 400
    assert _get(base, '/__data/graph-body?id=a')[0] == 400


def test_structure_has_independent_cap_and_body_keeps_normal_cap(graph_server, monkeypatch):
    _, base = graph_server
    import okf_loom.retrieval as retrieval
    monkeypatch.setattr(retrieval, 'MAX_RESPONSE_BYTES', 3000)
    assert _get(base)[0] == 413
    slim_response = _get(base, '/__data/graph.json?view=structure')
    assert slim_response[0] == 200
    revision = json.loads(slim_response[2])['bundle_revision']
    monkeypatch.setattr(retrieval, 'MAX_RESPONSE_BYTES', 256)
    body_response = _get(base, '/__data/graph-body?' + urlencode({'id': 'a', 'revision': revision}))
    assert body_response[0] == 413
    assert body_response[1]['Cache-Control'] == 'no-store'
    monkeypatch.setattr(transport, 'MAX_STRUCTURE_BYTES', 1500)
    status, headers, body = _get(base, '/__data/graph.json?view=structure')
    assert status == 413 and headers['Cache-Control'] == 'no-store'
    assert 'too large to display' in json.loads(body)['error']['message']
