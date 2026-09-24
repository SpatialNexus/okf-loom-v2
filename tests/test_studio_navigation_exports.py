import json
import re
import subprocess
from pathlib import Path

import pytest

from scripts.okf_loom.config import OkfConfig, OkfConfigError
from scripts.okf_loom.model import Bundle
from scripts.okf_loom.render import build_graph_data, build_site
from scripts.okf_loom.review_links import review_url


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / 'bundle'
    (root / 'sub').mkdir(parents=True)
    (root / 'index.md').write_text('# Home\n')
    (root / 'sub/index.md').write_text('# Directory\n')
    (root / 'sub/log.md').write_text('# History\n[Home](../index.md#home)\n')
    (root / 'sub/one.md').write_text('---\ntype: Note\n---\n# One\n'
        '[sibling](two.md#two) [dot](./two.md) [parent](../index.md#home) '
        '[index](index.md#directory) [log](log.md#history)\n')
    (root / 'sub/two.md').write_text('---\ntype: Note\n---\n# Two\n')
    return root


def metadata(page):
    return json.loads(re.search(r'id="okf-review-bootstrap">(.*?)</script>', page, re.S)[1])


def test_graph_navigation_documents_are_not_missing_or_nodes(bundle):
    model = Bundle.load(bundle)
    assert model.graph().unresolved == []
    assert len(model.graph().navigation) == 3
    data = build_graph_data(model)
    assert len(data['nodes']) == 2
    assert all(n['data']['unresolved_count'] == 0 for n in data['nodes'])
    assert 'href="/sub/two.md#two"' in data['bodies']['sub/one']
    assert 'href="/sub/two.md"' in data['bodies']['sub/one']
    assert 'href="/index.md#home"' in data['bodies']['sub/one']
    assert data['documents']['sub/log.md']['document'] == 'sub/log.md'
    with (bundle / 'sub/one.md').open('a') as f:
        f.write('\n[bad heading](index.md#absent) [missing](missing.md) [bad concept heading](two.md#absent)\n')
    broken = Bundle.load(bundle).graph().unresolved
    assert {link.target_raw for link in broken} == {'index.md#absent', 'missing.md', 'two.md#absent'}


def test_export_index_fragments_keep_authored_and_duplicate_headings(bundle, tmp_path):
    (bundle / 'index.md').write_text('# Home\nIntro\n\n# Home\nLater\n')
    out = tmp_path / 'site'
    build_site(Bundle.load(bundle), out, target='static')
    root = (out / 'index.html').read_text()
    assert len(re.findall(r'id="home"', root)) == 1
    assert len(re.findall(r'id="home-1"', root)) == 1
    directory = (out / 'sub/index.html').read_text()
    assert 'id="directory"' in directory


@pytest.mark.parametrize('base', [None, 'https://review.example.test/team/docs/'])
@pytest.mark.parametrize('target', ['static', 'single-file'])
def test_real_export_review_metadata_navigation_and_no_authority(bundle, tmp_path, base, target):
    if base:
        (bundle / 'okf-loom.config.yaml').write_text('viewer:\n  review_base_url: ' + base + '\n')
    from scripts.okf_loom.studio import Studio
    studio = Studio.for_bundle(bundle)
    studio.post_comment(concept='sub/one', body='never-export-comment-secret')
    studio.write_session_text('.token', 'never-export-session-secret')
    out = tmp_path / 'export'
    build_site(Bundle.load(bundle), out, target=target)
    page = (out / ('sub/one.html' if target == 'static' else 'index.html')).read_text()
    info = metadata(page)
    assert info['base_url'] == base
    assert info['read_only'] is True
    assert 'live Studio' in info['message']
    assert 'never-export-session-secret' not in page
    assert 'never-export-comment-secret' not in page
    if target == 'static':
        assert info['document'] == 'sub/one.md'
        assert info['url'] == (base + 'sub/one.md' if base else None)
        assert 'href="two.html#two"' in page
        assert 'href="../index.html#home"' in page
        assert 'href="log.html#history"' in page
        assert metadata((out / 'sub/log.html').read_text())['document'] == 'sub/log.md'
    assert review_url(base, 'sub/log.md', 'Heading & more') == (
        base + 'sub/log.md#Heading%20%26%20more' if base else None)


@pytest.mark.parametrize('value', ['javascript:alert(1)', '//review.test', 'https://user:secret@review.test/',
    'https://review.test/?token=secret', 'https://review.test/#secret', 'https://review.test/?',
    'https://review.test/../escape', 'https://review.test/%2e%2e/', 'https://review.test/\\evil',
    'https://review.test/\n', 'https://review.test:99999/', 'https://review.test:/',
    'https://bad..host/', 'https://review.test/"onload="x', '', True])
def test_untrusted_review_base_fails_config_and_build_cli(bundle, tmp_path, value):
    (bundle / 'okf-loom.config.yaml').write_text('viewer:\n  review_base_url: ' + json.dumps(value) + '\n')
    with pytest.raises(OkfConfigError):
        OkfConfig.load(bundle)
    out = tmp_path / 'export'
    command = Path(__file__).resolve().parents[1] / 'scripts/okf-loom'
    result = subprocess.run([str(command), 'build', str(bundle), '--target', 'static', '--out', str(out)], capture_output=True)
    assert result.returncode != 0
    assert not out.exists()
    assert b'user:secret' not in result.stderr


def test_reserved_log_links_reach_real_pages_in_every_mode(bundle, tmp_path):
    """A log is read from its own directory and links to pages that exist.

    Regression: the spa export kept canonical ``/x.md`` hrefs on log pages while
    every other spa page links concepts extensionless, so log links 404'd.
    """
    from scripts.okf_loom.render import _reserved_document_links
    (bundle / 'sub/log.md').write_text(
        '# History\n[one](one.md#one) [dot](./two.md) [home](../index.md#home) '
        '[dir](index.md) [self](#history) [gone](../nope/missing.md#x) [ext](https://e.x/a.md)\n')
    model = Bundle.load(bundle)
    html = ('<a href="one.md#one">one</a><a href="./two.md">dot</a><a href="../index.md#home">home</a>'
            '<a href="index.md">dir</a><a href="#history">self</a>'
            '<a href="../nope/missing.md#x">gone</a><a href="https://e.x/a.md">ext</a>')
    expected = {
        'serve': ['/sub/one#one', '/sub/two', '/#home', '/sub/', '#history'],
        'spa': ['/sub/one#one', '/sub/two', '/#home', '/sub/', '#history'],
        'static': ['one.html#one', 'two.html', '../index.html#home', 'index.html', '#history'],
    }
    for mode, hrefs in expected.items():
        out = _reserved_document_links(html, 'sub/log.md', mode, model)
        assert re.findall(r'href="([^"]*)"', out)[:5] == hrefs, mode
        # Diagnostics stay truthful: a missing target is flagged, never rewritten
        # into a plausible page; an external link is untouched.
        assert 'href="/nope/missing.md#x" class="okf-internal" data-okf-broken="1"' in out, mode
        assert 'href="https://e.x/a.md">ext' in out, mode
    for target in ('spa', 'static'):
        out_dir = tmp_path / target
        build_site(model, out_dir, target=target)
        main = (out_dir / 'sub/log.html').read_text().split('<main', 1)[1]
        links = dict((label, tag) for tag, label in re.findall(r'<a\b([^>]*)>([^<]*)</a>', main))
        stale = [tag for label, tag in links.items()
                 if label not in {'gone', 'ext'} and re.search(r'href="[^"]*\.md(?:#[^"]*)?"', tag)]
        assert not stale, (target, stale)
        # The shared renderer classifies before resolution; a link that resolved
        # inside the bundle (or to this page) must not open a new tab.
        for label in ('one', 'dot', 'home', 'dir', 'self'):
            assert 'class="okf-internal"' in links[label], (target, label)
            assert 'target="_blank"' not in links[label], (target, label)
        assert 'data-okf-broken="1"' in links['gone'], target
        assert 'target="_blank"' in links['ext'], target


def test_self_fragments_are_local_only_on_their_own_page(bundle, tmp_path):
    from scripts.okf_loom.render import render_concept_body_with_projection
    (bundle / 'sub/one.md').write_text('---\ntype: Note\n---\n# One\n'
        '[self](#tail) [relative-self](./one.md#tail) [absolute-self](/sub/one.md#tail) '
        '[sibling](two.md#two) [dot](./two.md#two) [absolute](/sub/two.md#two) '
        '[parent](../top.md#top) [missing](missing.md#lost) '
        '[external](https://example.test/sub/one.md#tail)\n\n## Tail\n')
    (bundle / 'top.md').write_text('---\ntype: Note\n---\n# Top\n')
    model = Bundle.load(bundle)
    assert any(link.target_raw == 'missing.md#lost' for link in model.graph().unresolved)

    def links(rendered):
        return {label: attrs for attrs, label in re.findall(r'<a\b([^>]*)>([^<]*)</a>', rendered)}

    def assert_page(rendered, mode):
        found = links(rendered)
        for label in ('self', 'relative-self', 'absolute-self'):
            assert 'href="#tail"' in found[label], (mode, label, found[label])
            assert 'target=' not in found[label]
        for label in ('sibling', 'dot', 'absolute'):
            expected = 'two.html#two' if mode == 'static' else '/sub/two#two'
            assert f'href="{expected}"' in found[label], (mode, label)
        parent = '../top.html#top' if mode == 'static' else '/top#top'
        assert f'href="{parent}"' in found['parent']
        # Consumers tolerate missing documents, but never turn them into a
        # plausible local fragment. Graph diagnostics retain the missing target.
        missing = 'missing.html#lost' if mode == 'static' else '/sub/missing#lost'
        assert f'href="{missing}"' in found['missing']
        assert 'href="https://example.test/sub/one.md#tail"' in found['external']
        assert 'target="_blank"' in found['external']

    for mode in ('serve', 'spa', 'static'):
        rendered, _ = render_concept_body_with_projection(model.concepts[('sub', 'one')], model, mode=mode)
        assert_page(rendered, mode)
    for target in ('static', 'single-file'):
        out = tmp_path / target
        build_site(model, out, target=target)
        if target == 'static':
            assert_page((out / 'sub/one.html').read_text(), 'static')
            data = build_graph_data(model)
        else:
            page = (out / 'index.html').read_text()
            data, _ = json.JSONDecoder().raw_decode(page.split('window.BUNDLE = ', 1)[1])
        graph_links = links(data['bodies']['sub/one'])
        for label in ('self', 'relative-self', 'absolute-self'):
            assert 'href="/sub/one.md#tail"' in graph_links[label], (target, label)
        for label in ('sibling', 'dot', 'absolute'):
            assert 'href="/sub/two.md#two"' in graph_links[label], (target, label)
        assert 'href="/top.md#top"' in graph_links['parent']


@pytest.mark.parametrize('href', ['../../one.md#tail', '//example.test/sub/one#tail',
    'javascript:alert(1)#tail', '/sub/one?token=x#tail', '../.okf-loom/secret.md#tail'])
def test_page_fragment_context_does_not_promote_invalid_or_external_links(href):
    from scripts.okf_loom.navigation import normalize_document_links, resolve_document_link
    assert resolve_document_link('sub/one.md', href) is None
    rendered = '<a href="' + href + '">x</a>'
    assert normalize_document_links(rendered, 'sub/one.md', 'serve', page_url='/sub/one') == rendered
