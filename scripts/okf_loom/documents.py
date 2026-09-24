"""Stable Markdown document identities, independent of graph membership."""
from pathlib import Path

from .paths import concept_id_from_str, validate_segment


def document_path(value: str) -> str:
    """Accept one canonical bundle-relative Markdown path, never a URL/alias."""
    if not isinstance(value, str) or not value.endswith('.md') or len(value) > 1024:
        raise ValueError('invalid document')
    for segment in value.split('/'):
        validate_segment(segment)
    if value in {'index.md', 'log.md'}:
        return value
    if Path(value).name not in {'index.md', 'log.md'}:
        concept_id_from_str(value[:-3])
    return value


def comment_document(row: dict) -> str:
    """Read new identity or derive legacy concept identity without rewriting it."""
    if 'document' in row:
        return document_path(row['document'])
    return '/'.join(concept_id_from_str(row.get('concept'))) + '.md'


def document_concept(value: str) -> str:
    value = document_path(value)
    return '' if Path(value).name in {'index.md', 'log.md'} else value[:-3]
