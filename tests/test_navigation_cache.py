"""Content-only navigation caches preserve source proof and live freshness."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import subprocess
import time
from urllib.request import urlopen

import pytest

from browser_support import live_server, TOOLKIT_ROOT
from okf_loom import Bundle
from okf_loom import navigation as nav
from okf_loom.model import Graph
from okf_loom.retrieval import RetrievalService
from okf_loom.viewer import markdown


@pytest.fixture(autouse=True)
def clear_navigation_caches():
    nav._cached_heading_fragments.cache_clear()
    nav._cached_section_headings.cache_clear()
    yield
    nav._cached_heading_fragments.cache_clear()
    nav._cached_section_headings.cache_clear()


def bundle_for(root, body):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'target.md').write_text('---\ntype: Reference\n---\n\n' + body, encoding='utf-8')
    return Bundle.load(root)


def test_render_work_reused_across_snapshots_but_source_proof_is_not(tmp_path, monkeypatch):
    body = '## Exact\nneedle\n'
    first = bundle_for(tmp_path / 'first', body)
    second = bundle_for(tmp_path / 'second', body)
    real = markdown.render_blocks_with_owners
    calls = []

    def counted(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(markdown, 'render_blocks_with_owners', counted)
    for bundle in (first, second, first):
        service = RetrievalService(bundle)
        passage = service.passages[0]
        assert service.verified_section_fragment('target', passage.passage_id,
                                                revision=service.revision) == 'exact'
        prove, label = nav._concept_section_prover(bundle.concepts[('target',)])
        assert prove(passage) == 'exact'
        assert label('exact') == 'Exact'
        assert prove(replace(passage, excerpt='forged')) is None
    assert calls == [first.concepts[('target',)].body]
    concept = second.concepts[('target',)]
    # Same body, different frontmatter offsets must still be re-proven.
    concept._adopt_raw_text(concept.raw_text.replace('type: Reference', 'type: Reference\ntitle: Extra'))
    second.invalidate()
    assert nav.passage_heading_fragment(concept, passage) is None
    current = RetrievalService(second)
    assert nav.passage_heading_fragment(concept, current.passages[0]) == 'exact'
    # A mismatched in-memory body cannot borrow a cached proof either.
    concept.body = '## Other\nneedle\n'
    assert nav._concept_section_prover(concept) is None
    assert calls == [first.concepts[('target',)].body]


def test_fragment_inventory_is_content_keyed_and_caller_owned(monkeypatch):
    real = markdown.markdown_to_html
    calls = []

    def counted(body):
        calls.append(body)
        return real(body)

    monkeypatch.setattr(markdown, 'markdown_to_html', counted)
    assert nav.heading_fragments('## Old\n') == {'old'}
    nav.heading_fragments('## Old\n').add('poison')
    assert nav.heading_fragments('## Old\n') == {'old'}
    assert nav.heading_fragments('## New\n') == {'new'}
    assert calls == ['## Old\n', '## New\n']


def test_cache_bounds_and_oversized_bypass(tmp_path, monkeypatch):
    for i in range(nav._NAVIGATION_CACHE_ENTRIES + 5):
        body = f'## Heading {i}\nneedle\n'
        nav.heading_fragments(body)
        nav._concept_section_prover(bundle_for(tmp_path, body).concepts[('target',)])
    caches = (nav._cached_heading_fragments, nav._cached_section_headings)
    assert all(cache.cache_info().currsize == nav._NAVIGATION_CACHE_ENTRIES for cache in caches)
    before = [cache.cache_info() for cache in caches]
    monkeypatch.setattr(nav, '_NAVIGATION_CACHE_BODY_CHARS', 8)
    concept = bundle_for(tmp_path, '## Oversized\nneedle\n').concepts[('target',)]
    assert nav.heading_fragments(concept.body) == {'oversized'}
    assert nav._concept_section_prover(concept)[1]('oversized') == 'Oversized'
    assert [cache.cache_info() for cache in caches] == before


def test_concurrent_misses_hits_and_evictions_publish_immutable_facts():
    def work(i):
        body = f'## Heading {i}\nneedle\n'
        result = nav.heading_fragments(body)
        assert result == {f'heading-{i}'}
        result.add('caller mutation')
        headings, unique, labels = nav._cached_section_headings(body)
        assert headings == ((0, f'heading-{i}'),)
        assert unique == frozenset({f'heading-{i}'})
        assert labels == ((f'heading-{i}', f'Heading {i}'),)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, [i % 80 for i in range(400)]))
    assert nav._cached_heading_fragments.cache_info().currsize <= nav._NAVIGATION_CACHE_ENTRIES
    assert nav._cached_section_headings.cache_info().currsize <= nav._NAVIGATION_CACHE_ENTRIES


def test_live_watcher_and_mutator_refresh_cached_navigation(tmp_path):
    root = tmp_path / 'bundle'
    bundle_for(root, '## Original\nneedle originalword\n')
    (root / 'source.md').write_text(
        '---\ntype: Reference\n---\n\n[Target](target.md#original)\n', encoding='utf-8')

    def get(base, route):
        with urlopen(base + route, timeout=30) as response:
            return response.read().decode('utf-8')

    def check(base, heading, word):
        assert f'id="{heading}"' in get(base, '/target')
        data = json.loads(get(base, '/__data/doc?id=target'))
        assert f'id="{heading}"' in data['html']
        assert word in data['raw'] and word in data['annotation_source']
        search = get(base, '/__search?q=' + word)
        assert f'okf-section={heading}' in search
        assert 'Open matching section' in search
        # Exercise other pages' fresh Graph.build as well as its diagnostic:
        # fragment validity must change even though source.md never changed.
        assert '/target#original' in get(base, '/source')
        get(base, '/__data/doc?id=source')
        graph = Graph.build(Bundle.load(root))
        assert bool(graph.unresolved) == (heading != 'original')

    def wait_for_snapshot(base, word):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if 'Open matching section' in get(base, '/__search?q=' + word):
                return
            time.sleep(0.05)
        pytest.fail('watcher did not publish the edited search snapshot')

    with live_server(root, '--no-watch-ui') as base:
        check(base, 'original', 'originalword')
        path = root / 'target.md'
        path.write_text(path.read_text().replace('Original', 'Watched').replace(
            'originalword', 'watchedword'), encoding='utf-8')
        # Canonical-source pages/data are fresh even before watcher publication.
        assert 'watchedword' in get(base, '/target')
        assert 'watchedword' in get(base, '/__data/doc?id=target')
        wait_for_snapshot(base, 'watchedword')
        check(base, 'watched', 'watchedword')
        subprocess.run([
            str(TOOLKIT_ROOT / 'scripts/okf-loom'), 'replace-text', '--bundle', str(root),
            '--id', 'target', '--old', '## Watched\nneedle watchedword',
            '--new', '## Mutated\nneedle mutatedword',
        ], check=True, capture_output=True, text=True, timeout=30)
        assert 'mutatedword' in get(base, '/target')
        assert 'mutatedword' in get(base, '/__data/doc?id=target')
        wait_for_snapshot(base, 'mutatedword')
        check(base, 'mutated', 'mutatedword')
