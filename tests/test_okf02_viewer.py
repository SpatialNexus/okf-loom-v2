"""OKF 0.2 renderer/portable contract, without backend mutation changes."""
import json
from datetime import datetime, timezone

import pytest
import yaml

from okf_loom.model import Bundle
from okf_loom.render import build_graph_data, _browser_passage_corpus, render_concept_body_with_projection
from okf_loom.viewer.metadata import render_metadata, source_anchor


def write_fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'notes').mkdir(exist_ok=True)
    (root / 'index.md').write_text('---\nokf: "0.2"\n---\nA disposable review bundle.\n')
    fm = {
        'type': 'Attested Computation', 'title': 'Evidence review',
        'description': 'Review the provenance before relying on this result.',
        'timestamp': '2020-01-01T00:00:00Z',
        'generated': {'by': 'agent:builder', 'at': '2026-09-01T12:00:00Z'},
        'verified': [{'by': 'human:reviewer', 'at': '2026-09-02T12:00:00Z'}],
        'status': 'draft', 'stale_after': '2026-09-08T12:00:00Z',
        'usage_window': {'from': '2026-08-01T00:00:00Z', 'to': '2026-09-01T00:00:00Z'},
        'sources': [
            {'id': 'alpha', 'title': 'Local evidence', 'resource': '/notes/source.md', 'author': 'Ada', 'usage_count': 0},
            {'id': 'beta', 'title': '<img src=x onerror=window.OKF_XSS=1>', 'resource': 'javascript:window.OKF_XSS=1', 'author': '<script>window.OKF_XSS=1</script>', 'usage_count': 7,
             'usage_window': {'from': '2026-07-01T00:00:00Z', 'to': '2026-07-02T00:00:00Z'}},
            {'id': 'scope', 'resource': 'all repositories in project:atlas'},
            {'id': 'web', 'title': 'External evidence', 'resource': 'https://example.org/evidence?a=1&b=2'},
            {'id': 'encoded', 'resource': 'javascript&colon;alert(1)'},
            {'id': 'protocol', 'resource': '//example.org/tracker'},
        ],
        'runtime': 'python', 'parameters': [{'name': 'query', 'type': 'string', 'required': True}],
        'computation': '</script><script>window.OKF_XSS=1</script>',
        'executor': {'resource': 'javascript:window.OKF_XSS=1', 'receipt': ['result']},
        'attester': {'resource': 'https://example.org/DO-NOT-FETCH'},
        'receipt': {'result': '<svg onload=window.OKF_XSS=1>'},
        'citations': [{'id': 'alpha', 'text': 'Separate Loom citation'}],
        'graph_cluster': 'Existing grouping',
    }
    body = ('\n# Findings\n\nClaim with source[^alpha] and hostile source[^beta].\n\n'
            'A normal footnote[^ordinary].\n\n[^ordinary]: Authored footnote.\n\n'
            '```python\nprint("Highlight me")\n```\n\n```mermaid\ngraph LR\n A --> B\n```\n\n```math\nx^2 + y^2\n```\n')
    path = root / 'notes/review.md'
    path.write_text('---\n' + yaml.safe_dump(fm, sort_keys=False) + '---\n' + body)
    (root / 'notes/source.md').write_text('---\ntype: Note\ntitle: Local evidence\n---\nSource material.\n')
    return fm, body, path


def test_metadata_rendering_identity_safety_and_projection(tmp_path):
    fm, body, path = write_fixture(tmp_path / 'bundle')
    bundle = Bundle.load(path.parents[1])
    concept = bundle.concepts[('notes', 'review')]
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    html = render_metadata(concept, bundle, mode='static', now=now)
    assert '>Stale</span>' in html
    assert 'Trust (advisory): <strong>human-reviewed' in html
    assert 'Legacy timestamp (not generation time)' in html
    assert 'href="source.html"' in html
    assert 'href="javascript' not in html and '<img' not in html and '<script' not in html
    assert 'Metadata only — never executed' in html
    rendered, projection = render_concept_body_with_projection(concept, bundle, mode='serve')
    assert projection['complete']
    assert 'href="#' + source_anchor(concept, 'alpha') + '"' in rendered
    assert 'href="#fn-ordinary"' in rendered
    fm['sources'].reverse()
    path.write_text('---\n' + yaml.safe_dump(fm, sort_keys=False) + '---\n' + body)
    new = Bundle.load(bundle.root)
    after, after_projection = render_concept_body_with_projection(new.concepts[concept.id], new, mode='serve')
    assert after == rendered
    assert after_projection['complete']
    graph = build_graph_data(new)
    node = next(n['data'] for n in graph['nodes'] if n['data']['id'] == 'notes/review')
    assert node['metadata'] == {'graph_cluster': 'Existing grouping'}
    assert node['okf_metadata']['trust_tier'] == 'human-reviewed'
    source_relation = graph['edges'][0]['data']['relations'][0]
    assert source_relation['origin'] == 'sources'
    assert source_relation['type'] == 'derived_from'
    assert source_relation['source_id'] == 'alpha'
    corpus = _browser_passage_corpus(new, [])
    assert corpus['manifest']['concepts'][0]['metadata']['trust_tier'] == 'human-reviewed'
    assert json.loads(corpus['manifest']['graph']['canonical_edges'])[0]['via'] == 'sources'


@pytest.mark.parametrize('expiry, expected', [
    ('2026-09-08T12:00:00Z', 'Stale'),
    ('2026-09-08T12:00:01Z', 'Not stale'),
    ('2026-02-30T12:00:00Z', 'Invalid expiry — freshness unknown'),
    ('2026-09-08', 'Invalid expiry — freshness unknown'),
    (None, 'No expiry recorded'),
])
def test_expiry_uses_core_validation_and_inclusive_boundary(tmp_path, expiry, expected):
    fm, body, path = write_fixture(tmp_path / 'bundle')
    if expiry is None:
        fm.pop('stale_after')
    else:
        fm['stale_after'] = expiry
    path.write_text('---\n' + yaml.safe_dump(fm) + '---\n' + body)
    b = Bundle.load(path.parents[1])
    html = render_metadata(b.concepts[('notes','review')], b, now=datetime(2026,9,8,12,tzinfo=timezone.utc))
    assert '>' + expected + '</span>' in html


def test_duplicate_source_ids_do_not_join(tmp_path):
    fm, body, path = write_fixture(tmp_path / 'bundle')
    fm['sources'].append(dict(fm['sources'][0]))
    path.write_text('---\n' + yaml.safe_dump(fm) + '---\n' + body)
    b = Bundle.load(path.parents[1]); c = b.concepts[('notes','review')]
    html, _ = render_concept_body_with_projection(c, b, mode='serve')
    assert 'data-okf-source-ref="alpha"' not in html
    assert 'duplicate id; attribution unavailable' in render_metadata(c, b)
