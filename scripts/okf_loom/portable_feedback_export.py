"""Export side of portable page-level feedback notes (roadmap X2).

``build``/``render`` with ``--include-portable-feedback`` (static and
single-file targets only) add an offline composer to every concept page. The
reader writes one note and downloads it as a file; nothing is sent anywhere.
A local Studio operator can later import that file
(``portable_feedback.parse_note`` is the authoritative parser).

Each note targets the SHA-256 of the page's canonical document **exact
on-disk bytes**, the same revision the import route re-checks. It is never the
hash of newline-normalized loaded text and never an annotation/section
revision. The bytes are read through
:func:`canonical_source.read_canonical_document_bytes` (stable, no-follow,
visibility-checked). They must decode strictly as UTF-8 and, after the
loader's universal-newline normalization (CRLF and lone CR become LF), equal
the ``raw_text`` the page was rendered from. Otherwise the file changed or
could not be read after the bundle was loaded. That page then gets no
composer, a warning goes to stderr, and the skip is counted in the build
stats. The build itself does not fail.

Nothing here changes default output: callers only use it when the flag is on.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from .canonical_source import read_canonical_document_bytes
from .documents import document_concept, document_path
from .paths import concept_id_to_str

#: Opt-in viewer asset. It is excluded from the default ``__static`` copy.
ASSET_NAME = "portable-feedback.js"
BOOTSTRAP_ID = "okf-portable-feedback"
BOOTSTRAP_VERSION = 1

_SKIP_MESSAGES = {
    "changed": "its source changed after the bundle was loaded",
    "missing": "its source file is missing or not visible",
    "unreadable": "its source could not be read safely",
    "not_utf8": "its source is not valid UTF-8",
    "invalid_document": "its path is not a canonical concept document",
}


@dataclass(frozen=True)
class PortableFeedbackTargets:
    """Per-concept note targets plus the pages that were skipped."""

    #: concept id -> ``{"document", "source_sha256"}``
    targets: dict[str, dict[str, str]]
    #: ``(document, reason)`` pairs, sorted by document
    skipped: tuple[tuple[str, str], ...]

    def stats(self) -> dict[str, int]:
        return {"portable_feedback_pages": len(self.targets),
                "portable_feedback_skipped": len(self.skipped)}


def _universal_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def exact_source_target(bundle: Any, concept: Any) -> tuple[dict[str, str] | None, str | None]:
    """Return ``(target, None)`` or ``(None, reason)`` for one concept."""
    document = concept_id_to_str(concept.id) + ".md"
    try:
        if document_path(document) != document or not document_concept(document):
            return None, "invalid_document"
    except (TypeError, ValueError):
        return None, "invalid_document"
    try:
        raw = read_canonical_document_bytes(bundle, document)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unreadable"
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, "not_utf8"
    if _universal_newlines(text) != concept.raw_text:
        return None, "changed"
    return {"document": document, "source_sha256": hashlib.sha256(raw).hexdigest()}, None


def collect_targets(bundle: Any, *, stream: TextIO | None = None) -> PortableFeedbackTargets:
    """Hash every concept's exact bytes; warn on (and count) every skip."""
    stream = sys.stderr if stream is None else stream
    targets: dict[str, dict[str, str]] = {}
    skipped: list[tuple[str, str]] = []
    for cid in sorted(bundle.concepts):
        concept = bundle.concepts[cid]
        target, reason = exact_source_target(bundle, concept)
        if target is not None:
            targets[concept_id_to_str(cid)] = target
            continue
        document = concept_id_to_str(cid) + ".md"
        skipped.append((document, str(reason)))
        print(f"warning: portable feedback skipped for {document}: "
              f"{_SKIP_MESSAGES.get(str(reason), str(reason))}", file=stream)
    return PortableFeedbackTargets(targets=targets, skipped=tuple(skipped))


def _script_json(payload: dict[str, Any]) -> str:
    # Non-executable JSON inside <script>: escape every character that could
    # end the element or open markup, exactly like the review bootstrap.
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def page_bootstrap_script(target: dict[str, str]) -> str:
    """One static page's ``{version, document, source_sha256}`` bootstrap."""
    payload = {"version": BOOTSTRAP_VERSION, "document": target["document"],
               "source_sha256": target["source_sha256"]}
    return (f'<script type="application/json" id="{BOOTSTRAP_ID}">'
            f'{_script_json(payload)}</script>')


def single_file_bootstrap_script(targets: dict[str, dict[str, str]]) -> str:
    """The single-file map keyed by concept id."""
    payload = {"version": BOOTSTRAP_VERSION,
               "documents": {cid: {"document": target["document"],
                                   "source_sha256": target["source_sha256"]}
                             for cid, target in sorted(targets.items())}}
    return (f'<script type="application/json" id="{BOOTSTRAP_ID}">'
            f'{_script_json(payload)}</script>')


def inject_before_head_close(html: str, script: str) -> str:
    """Insert ``script`` before the first ``</head>``.

    Call this on FINISHED page HTML, after every template placeholder has been
    substituted, so a document path can never be mistaken for a placeholder.
    The first ``</head>`` of finished output is always the real one: head
    text is escaped, and body content comes after it.
    """
    if "</head>" not in html:
        raise ValueError("page has no </head> to carry the portable feedback bootstrap")
    return html.replace("</head>", script + "</head>", 1)
