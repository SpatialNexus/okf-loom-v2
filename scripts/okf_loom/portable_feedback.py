"""Portable page-level feedback notes (roadmap X2): closed parser + import helpers.

A portable note is ONE page-level JSON file written offline from a static or
single-file export and later imported by a local Studio operator. The origin
is unauthenticated: anyone can write or edit a note file, and byte-identical
page copies are indistinguishable. Nothing here authenticates a person; the
server only proves that a local operator explicitly imported these exact note
bytes against the exact on-disk bytes of one canonical document.

Note format v1 (closed; unknown/duplicate keys are rejected)::

    {"format": "okf-loom.portable-feedback", "version": 1,
     "note_id": "<32 lowercase hex>",
     "target": {"document": "tables/orders.md", "source_sha256": "<64 lowercase hex>"},
     "body": "...", "quote": "...", "claimed_author": "...",
     "claimed_at": "2026-09-22T10:15:00.000Z"}

``quote``, ``claimed_author`` and ``claimed_at`` are optional; when present
they must be non-empty strings (``null`` is rejected). Strings are stored
exactly as received: no Unicode normalization and no trimming.

This module is stdlib-only and side-effect free.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

NOTE_FORMAT = "okf-loom.portable-feedback"
NOTE_VERSION = 1
MAX_NOTE_BYTES = 32 * 1024
MAX_BODY_CODE_POINTS = 8000
MAX_QUOTE_CODE_POINTS = 1000
MAX_CLAIMED_AUTHOR_CODE_POINTS = 80
#: Idempotency-key namespace owned by the import route. Ordinary
#: ``POST /__comment`` requests must not use it (see server.py).
IMPORT_KEY_PREFIX = "pfn."
#: Fingerprint-request discriminator stored (hashed) in the create WAL.
FINGERPRINT_KIND = "portable_feedback_import"
MARKER_SCHEMA = 1
PREVIEW_TOKEN_VERSION = 1
PREVIEW_TOKEN_TTL_SECONDS = 600
_MAX_PREVIEW_TOKEN_CHARS = 1024

_REQUIRED_KEYS = frozenset({"format", "version", "note_id", "target", "body"})
_OPTIONAL_KEYS = frozenset({"quote", "claimed_author", "claimed_at"})
_TARGET_KEYS = frozenset({"document", "source_sha256"})
# Explicit ASCII classes: ``\d`` would also match non-ASCII digits.
_NOTE_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CLAIMED_AT = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]{3})?Z")
_B64URL = re.compile(r"[A-Za-z0-9_-]+")
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


class PortableNoteError(ValueError):
    """A note was refused. ``reason`` is a stable, low-cardinality code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PortableNote:
    note_id: str
    document: str
    source_sha256: str
    body: str
    quote: str | None
    claimed_author: str | None
    claimed_at: str | None
    note_sha256: str

    def echo(self) -> dict[str, Any]:
        """The note's fields for preview display (all untrusted text)."""
        out: dict[str, Any] = {
            "note_id": self.note_id, "document": self.document,
            "source_sha256": self.source_sha256, "body": self.body,
            "note_sha256": self.note_sha256,
        }
        for name in ("quote", "claimed_author", "claimed_at"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


def _closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PortableNoteError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise PortableNoteError("json")


#: A valid note nests two levels deep (note -> target). Anything past this
#: is refused before ``json.loads`` so the outcome never depends on the
#: interpreter's recursion limit (3.11 raises where 3.12 still parses).
_MAX_JSON_DEPTH = 16


def _nested_too_deep(text: str) -> bool:
    depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def _well_formed(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _text_ok(value: str) -> bool:
    """Body/quote: reject C0 controls other than LF and TAB."""
    return not any(ord(ch) < 0x20 and ch not in "\n\t" for ch in value)


def _label_ok(value: str) -> bool:
    """claimed_author: reject C0, DEL, C1 and bidi embedding/isolate controls."""
    return not any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in _BIDI_CONTROLS
                   for ch in value)


def _optional_text(data: dict[str, Any], name: str, limit: int, check) -> str | None:
    if name not in data:
        return None
    value = data[name]
    if (type(value) is not str or not value or len(value) > limit
            or not _well_formed(value) or not check(value)):
        raise PortableNoteError(name)
    return value


def canonical_note_bytes(parsed: dict[str, Any]) -> bytes:
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def parse_note(raw: bytes) -> PortableNote:
    """Strictly parse one portable note; raise :class:`PortableNoteError`."""
    from .documents import document_concept, document_path

    if not isinstance(raw, (bytes, bytearray)):
        raise PortableNoteError("encoding")
    raw = bytes(raw)
    if len(raw) > MAX_NOTE_BYTES:
        raise PortableNoteError("too_large")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PortableNoteError("bom")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PortableNoteError("encoding") from exc
    if _nested_too_deep(text):
        raise PortableNoteError("not_object")
    try:
        data = json.loads(text, object_pairs_hook=_closed_pairs,
                          parse_constant=_reject_constant)
    except PortableNoteError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PortableNoteError("json") from exc
    if not isinstance(data, dict):
        raise PortableNoteError("not_object")
    keys = set(data)
    if not _REQUIRED_KEYS <= keys or not keys <= (_REQUIRED_KEYS | _OPTIONAL_KEYS):
        raise PortableNoteError("keys")
    if data["format"] != NOTE_FORMAT or type(data["format"]) is not str:
        raise PortableNoteError("format")
    if type(data["version"]) is not int or data["version"] != NOTE_VERSION:
        raise PortableNoteError("version")
    note_id = data["note_id"]
    if type(note_id) is not str or not _NOTE_ID.fullmatch(note_id):
        raise PortableNoteError("note_id")
    target = data["target"]
    if not isinstance(target, dict) or set(target) != _TARGET_KEYS:
        raise PortableNoteError("target")
    document = target["document"]
    if type(document) is not str or not _well_formed(document):
        raise PortableNoteError("document")
    try:
        if document_path(document) != document or not document_concept(document):
            raise PortableNoteError("document")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, PortableNoteError):
            raise
        raise PortableNoteError("document") from exc
    source_sha256 = target["source_sha256"]
    if type(source_sha256) is not str or not _SHA256.fullmatch(source_sha256):
        raise PortableNoteError("source_sha256")
    body = data["body"]
    if (type(body) is not str or not body or len(body) > MAX_BODY_CODE_POINTS
            or not body.strip() or not _well_formed(body) or not _text_ok(body)):
        raise PortableNoteError("body")
    quote = _optional_text(data, "quote", MAX_QUOTE_CODE_POINTS, _text_ok)
    claimed_author = _optional_text(
        data, "claimed_author", MAX_CLAIMED_AUTHOR_CODE_POINTS, _label_ok)
    claimed_at = _optional_text(data, "claimed_at", 64, lambda _v: True)
    if claimed_at is not None:
        match = _CLAIMED_AT.fullmatch(claimed_at)
        try:
            if match is None:
                raise ValueError
            datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
        except ValueError as exc:
            raise PortableNoteError("claimed_at") from exc
    return PortableNote(
        note_id=note_id, document=document, source_sha256=source_sha256,
        body=body, quote=quote, claimed_author=claimed_author,
        claimed_at=claimed_at,
        note_sha256=hashlib.sha256(canonical_note_bytes(data)).hexdigest(),
    )


def import_idempotency_key(note: PortableNote) -> str:
    return IMPORT_KEY_PREFIX + note.note_id


def fingerprint_request(note: PortableNote) -> dict[str, Any]:
    """The request object whose canonical hash is the create-WAL fingerprint."""
    return {"kind": FINGERPRINT_KIND, "note_sha256": note.note_sha256,
            "document": note.document, "source_sha256": note.source_sha256}


def build_marker(note: PortableNote) -> dict[str, Any]:
    """Server-owned ``portable_import`` row marker (deterministic; no import time)."""
    marker: dict[str, Any] = {
        "schema": MARKER_SCHEMA, "note_id": note.note_id,
        "note_sha256": note.note_sha256, "document": note.document,
        "source_sha256": note.source_sha256,
    }
    for name in ("claimed_author", "claimed_at", "quote"):
        value = getattr(note, name)
        if value is not None:
            marker[name] = value
    return marker


def new_preview_key() -> bytes:
    """A fresh per-process HMAC key; every token dies with the process."""
    return secrets.token_bytes(32)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    if not _B64URL.fullmatch(value):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_id(csrf_token: str) -> str:
    return hashlib.sha256(csrf_token.encode("utf-8")).hexdigest()[:16]


def sign_preview_token(key: bytes, *, csrf_token: str, note: PortableNote,
                       now: float | None = None,
                       ttl: int = PREVIEW_TOKEN_TTL_SECONDS) -> tuple[str, int]:
    """Return ``(token, exp)`` binding session, note bytes and exact target bytes."""
    exp = int(time.time() if now is None else now) + int(ttl)
    payload = json.dumps({
        "v": PREVIEW_TOKEN_VERSION, "sid": _session_id(csrf_token),
        "nsha": note.note_sha256, "doc": note.document,
        "ssha": note.source_sha256, "exp": exp,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    mac = hmac.new(key, payload, hashlib.sha256).digest()
    return _b64url(payload) + "." + _b64url(mac), exp


def verify_preview_token(key: bytes, token: Any, *, csrf_token: str,
                         note: PortableNote, now: float | None = None) -> bool:
    """Fail closed unless ``token`` is this process's unexpired token for ``note``."""
    if (not isinstance(token, str) or not token
            or len(token) > _MAX_PREVIEW_TOKEN_CHARS or token.count(".") != 1):
        return False
    encoded_payload, encoded_mac = token.split(".")
    try:
        payload = _unb64url(encoded_payload)
        mac = _unb64url(encoded_mac)
    except ValueError:
        return False
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return False
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(claims, dict) or set(claims) != {"v", "sid", "nsha", "doc", "ssha", "exp"}:
        return False
    exp = claims["exp"]
    current = time.time() if now is None else now
    return bool(
        claims["v"] == PREVIEW_TOKEN_VERSION and type(claims["v"]) is int
        and type(exp) is int and current <= exp
        # The MAC is already verified, so the claims are server-authored.
        and claims["sid"] == _session_id(csrf_token)
        and claims["nsha"] == note.note_sha256
        and claims["doc"] == note.document
        and claims["ssha"] == note.source_sha256
    )
