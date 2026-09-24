"""Credential-free portable review metadata consumed by viewer integrations."""
import json
from urllib.parse import quote

from .config import validate_review_base_url
from .documents import document_path


def review_url(base: str | None, document: str, fragment: str = '') -> str | None:
    base = validate_review_base_url(base)
    document = document_path(document)
    if base is None:
        return None
    return base + quote(document, safe='/') + ('#' + quote(fragment, safe='') if fragment else '')


def review_metadata(base: str | None, document: str | None = None) -> dict:
    base = validate_review_base_url(base)
    return {'schema': 1, 'base_url': base, 'document': document,
            'url': review_url(base, document) if document else None,
            'read_only': True,
            'message': 'This export is read-only. Comments require a live Studio session.'}


def inject_review_metadata(rendered: str, base: str | None, document: str | None = None) -> str:
    payload = json.dumps(review_metadata(base, document), ensure_ascii=True).replace('<', '\\u003c')
    return rendered.replace('</head>', '<script type="application/json" id="okf-review-bootstrap">'
                            + payload + '</script></head>', 1)
