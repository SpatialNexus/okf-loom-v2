"""Source proof only: no adapter slug guesses or changed retrieval wire records."""
from dataclasses import replace
from html.parser import HTMLParser

import pytest

from okf_loom import Bundle
from okf_loom.navigation import passage_heading_fragment
from okf_loom.retrieval import RetrievalService, SearchRequest, build_passage_corpus
from okf_loom.viewer.markdown import markdown_to_html


class HeadingDOM(HTMLParser):
    def __init__(self, rendered):
        super().__init__(convert_charrefs=True)
        self.headings = []
        self.current = None
        self.feed(rendered)

    def handle_starttag(self, tag, attrs):
        if tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.current = [dict(attrs).get('id'), '']

    def handle_data(self, text):
        if self.current is not None:
            self.current[1] += text

    def handle_endtag(self, tag):
        if tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'} and self.current is not None:
            self.headings.append(tuple(self.current))
            self.current = None


def service_for(tmp_path, body, *, newline='\n', frontmatter=True):
    path = tmp_path / 'nested' / 'cafe.md'
    path.parent.mkdir(exist_ok=True)
    raw = ('---\ntype: Reference\ntitle: Example\n---\n\n' if frontmatter else '') + body
    raw = raw.replace('\n', newline)
    path.write_text(raw, encoding='utf-8', newline='')
    bundle = Bundle.load(tmp_path)
    concept = bundle.concepts[('nested', 'cafe')]
    # Bundle.load uses universal-newline reading. Also prove direct snapshots
    # that retain CRLF, rather than claiming a served fixture preserves it.
    concept._adopt_raw_text(raw)
    bundle.invalidate()
    return RetrievalService(bundle), concept


def matched(service):
    rows, revision = service.search_with_revision(SearchRequest(query='needle', mode='lexical'))
    assert len(rows) == 1
    return rows[0].evidence['passage'], revision


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('heading', [
    'A plain section', 'Café 日本語 & "quoted"',
    '**Strong** and `code` with [link](/other.md)', 'Closing hashes ###',
])
def test_matching_section_uses_actual_renderer_identity(tmp_path, newline, heading):
    service, concept = service_for(tmp_path, '# Intro\nOrdinary text.\n\n## ' + heading
                                   + '\nneedle exact passage\n', newline=newline)
    before = build_passage_corpus(service.bundle)
    passage, revision = matched(service)
    fragment = service.verified_section_fragment('nested/cafe', passage['passage_id'], revision=revision)
    dom = HeadingDOM(markdown_to_html(concept.body))
    assert fragment is not None
    assert fragment == dom.headings[1][0]
    assert sum(own_id == fragment for own_id, _ in dom.headings) == 1
    assert passage['excerpt'] == 'needle exact passage'
    assert build_passage_corpus(service.bundle) == before
    assert matched(service) == (passage, revision)


def test_duplicate_headings_map_by_source_owner_not_heading_text(tmp_path):
    service, concept = service_for(tmp_path, '# Repeated\nordinary\n\n# Repeated\nneedle\n')
    passage, revision = matched(service)
    dom = HeadingDOM(markdown_to_html(concept.body))
    assert dom.headings[0][0] != dom.headings[1][0]
    assert service.verified_section_fragment('nested/cafe', passage['passage_id'], revision=revision) == dom.headings[1][0]


@pytest.mark.parametrize('body', ['# needle\n', '\n\n## needle\n', '# needle'])
def test_heading_only_passage_and_leading_blank_offsets(tmp_path, body):
    service, concept = service_for(tmp_path, body)
    passage, revision = matched(service)
    dom = HeadingDOM(markdown_to_html(concept.body))
    assert service.verified_section_fragment('nested/cafe', passage['passage_id'], revision=revision) == dom.headings[0][0]


@pytest.mark.parametrize('body', [
    'needle preamble\n\n# Later\nordinary\n',
    'needle without heading\n',
    '```markdown\n# Fake\nneedle\n```\n',
    '~~~\n# Fake\nneedle\n',
    '    # Indented code\n    needle\n',
    '> # Quoted\n> needle\n',
    '#No heading\nneedle\n',
    '  ## Indented heading\nneedle\n',
    'Setext\n------\nneedle\n',
    '# Repeated\nordinary\n# Repeated\nneedle\n# Repeated-1\nordinary\n',
    '> # Repeated\n\n# Repeated\nneedle\n',
    '# fn-note\nneedle[^note]\n\n[^note]: reference\n',
    '# Earlier\nordinary\n\nSetext\n------\nneedle\n',
    '# Before\ntext\u2028# After\nneedle\n',
    '# Before\ntext\r# After\nneedle\n',
])
def test_unavailable_ambiguous_or_unaligned_section_is_omitted(tmp_path, body):
    service, _ = service_for(tmp_path, body)
    passage = next(row for row in service.passages if 'needle' in row.excerpt)
    assert service.verified_section_fragment('nested/cafe', passage.passage_id, revision=service.revision) is None


def test_code_fence_heading_does_not_steal_following_real_heading(tmp_path):
    service, concept = service_for(tmp_path, '```\n# Same\n```\n\n# Same\nneedle\n')
    passage, revision = matched(service)
    dom = HeadingDOM(markdown_to_html(concept.body))
    assert len(dom.headings) == 1
    assert service.verified_section_fragment('nested/cafe', passage['passage_id'], revision=revision) == dom.headings[0][0]


def test_long_line_unicode_character_chunk_retains_source_owned_heading(tmp_path):
    service, concept = service_for(tmp_path, '# 日本語\n' + 'é' * 2500 + ' needle\n', frontmatter=False)
    passage, revision = matched(service)
    assert passage['char_start'] is not None
    dom = HeadingDOM(markdown_to_html(concept.body))
    assert service.verified_section_fragment('nested/cafe', passage['passage_id'], revision=revision) == dom.headings[0][0]


@pytest.mark.parametrize('concept_id', ['', ' ', '../nested/cafe', '/nested/cafe',
                                      'nested/cafe.md', 'nested//cafe', '__proto__',
                                      'other', None, {}, True])
def test_missing_malformed_or_cross_concept_target_fails_closed(tmp_path, concept_id):
    service, _ = service_for(tmp_path, '# Exact\nneedle\n')
    passage, revision = matched(service)
    assert service.verified_section_fragment(concept_id, passage['passage_id'], revision=revision) is None


@pytest.mark.parametrize('passage_id', ['', ' ', '__proto__', 'other:L7-L7', None, {}, True])
def test_missing_or_malformed_passage_fails_closed(tmp_path, passage_id):
    service, _ = service_for(tmp_path, '# Exact\nneedle\n')
    assert service.verified_section_fragment('nested/cafe', passage_id, revision=service.revision) is None


@pytest.mark.parametrize('revision', ['', ' ', None, {}, True, '0' * 64])
def test_missing_or_malformed_revision_fails_closed(tmp_path, revision):
    service, _ = service_for(tmp_path, '# Exact\nneedle\n')
    passage, _ = matched(service)
    assert service.verified_section_fragment('nested/cafe', passage['passage_id'], revision=revision) is None


def test_changed_revision_cannot_reuse_same_passage_coordinates(tmp_path):
    service, concept = service_for(tmp_path, '# Original\nneedle\n')
    passage, revision = matched(service)
    concept._adopt_raw_text(concept.raw_text.replace('# Original', '# Changed'))
    service.bundle.invalidate()
    current, current_revision = matched(service)
    assert passage['passage_id'] == current['passage_id']
    assert current_revision != revision
    assert service.verified_section_fragment('nested/cafe', passage['passage_id'], revision=revision) is None
    dom = HeadingDOM(markdown_to_html(concept.body))
    assert service.verified_section_fragment('nested/cafe', current['passage_id'], revision=current_revision) == dom.headings[0][0]


def test_existing_other_concept_cannot_consume_passage_identity(tmp_path):
    (tmp_path / 'other.md').write_text('---\ntype: Reference\n---\n# Exact\nneedle\n')
    service, _ = service_for(tmp_path, '# Exact\nneedle\n')
    passage = next(row for row in service.passages if row.concept_id == ('nested', 'cafe'))
    assert service.verified_section_fragment('other', passage.passage_id, revision=service.revision) is None


@pytest.mark.parametrize('changes', [
    {'concept_id': ('other',)}, {'path': 'other.md'}, {'heading_line': None},
    {'heading_line': 2}, {'line_start': 0}, {'line_end': 500},
    {'excerpt': 'not the exact passage'}, {'char_start': 0}, {'char_end': 500},
])
def test_internal_source_proof_rejects_mismatched_passage(tmp_path, changes):
    service, concept = service_for(tmp_path, '# Exact\nneedle\n')
    passage = service.passages[0]
    assert passage_heading_fragment(concept, replace(passage, **changes)) is None
