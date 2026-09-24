"""X2 portable feedback: closed note parser, note hash, and preview tokens."""
from __future__ import annotations

import base64
import json

import pytest

from scripts.okf_loom.portable_feedback import (
    MAX_NOTE_BYTES,
    PortableNoteError,
    build_marker,
    fingerprint_request,
    import_idempotency_key,
    new_preview_key,
    parse_note,
    sign_preview_token,
    verify_preview_token,
)

NOTE_ID = "0123456789abcdef0123456789abcdef"
SHA = "ab" * 32


def note(**changes):
    value = {
        "format": "okf-loom.portable-feedback", "version": 1, "note_id": NOTE_ID,
        "target": {"document": "tables/orders.md", "source_sha256": SHA},
        "body": "Please clarify the grain.",
    }
    for key, item in changes.items():
        if item is _DROP:
            value.pop(key, None)
        else:
            value[key] = item
    return value


_DROP = object()


def raw(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def reason_of(data: bytes) -> str:
    with pytest.raises(PortableNoteError) as info:
        parse_note(data)
    return info.value.reason


def test_minimal_and_full_notes_parse_exactly_without_normalization():
    parsed = parse_note(raw(note()))
    assert (parsed.note_id, parsed.document, parsed.source_sha256) == (NOTE_ID, "tables/orders.md", SHA)
    assert parsed.quote is parsed.claimed_author is parsed.claimed_at is None
    full = parse_note(raw(note(body="  e\u0301 keep\n\tspacing  ", quote="the grain",
                               claimed_author="Ann \u00e9", claimed_at="2026-09-22T10:15:00.000Z")))
    assert full.body == "  e\u0301 keep\n\tspacing  "
    assert full.claimed_author == "Ann \u00e9"
    assert parse_note(raw(note(claimed_at="2026-09-22T10:15:00Z"))).claimed_at == "2026-09-22T10:15:00Z"


@pytest.mark.parametrize("data, reason", [
    (b"\xef\xbb\xbf" + raw(note()), "bom"),
    (b'{"body": "\xff"}', "encoding"),
    (b"[1, 2]", "not_object"),
    (b"not json", "json"),
    (b"[" * 20000 + b"]" * 20000, "too_large"),
    (b"[" * 5000 + b"]" * 5000, "not_object"),  # deep nesting fails closed, never crashes
    (b'{"a": ' + b"[" * 5000 + b"]" * 5000 + b"}", "not_object"),  # same reason on every Python
    (raw(note())[:-1] + b', "version": 1}', "duplicate_key"),
    (raw(note(target={"document": "tables/orders.md", "source_sha256": SHA}))
     .replace(b'"source_sha256"', b'"document": "x.md", "source_sha256"', 1), "duplicate_key"),
    (raw(note()).replace(b'"version": 1', b'"version": NaN'), "json"),
    (raw(note()).replace(b'"version": 1', b'"version": Infinity'), "json"),
    (raw(note(extra="x")), "keys"),
    (raw(note(body=_DROP)), "keys"),
    (raw(note(format="okf-loom.other")), "format"),
    (raw(note(version=True)), "version"),
    (raw(note(version=1.0)), "version"),
    (raw(note(version=2)), "version"),
    (raw(note(note_id=NOTE_ID.upper())), "note_id"),
    (raw(note(note_id=NOTE_ID[:-1])), "note_id"),
    (raw(note(note_id="\u0660" * 32)), "note_id"),
    (raw(note(target={"document": "tables/orders.md"})), "target"),
    (raw(note(target={"document": "tables/orders.md", "source_sha256": SHA, "x": 1})), "target"),
    (raw(note(target={"document": "index.md", "source_sha256": SHA})), "document"),
    (raw(note(target={"document": "sub/log.md", "source_sha256": SHA})), "document"),
    (raw(note(target={"document": "../secret.md", "source_sha256": SHA})), "document"),
    (raw(note(target={"document": "/abs.md", "source_sha256": SHA})), "document"),
    (raw(note(target={"document": "tables/orders", "source_sha256": SHA})), "document"),
    (raw(note(target={"document": "tables/orders.md", "source_sha256": SHA.upper()})), "source_sha256"),
    (raw(note(body="")), "body"),
    (raw(note(body="   \n")), "body"),
    (raw(note(body="x" * 8001)), "body"),
    (raw(note(body="bell\x07")), "body"),
    (raw(note(body="cr\r")), "body"),
    (raw(note(body=None)), "body"),
    (b'{"format":"okf-loom.portable-feedback","version":1,"note_id":"' + NOTE_ID.encode()
     + b'","target":{"document":"tables/orders.md","source_sha256":"' + SHA.encode()
     + b'"},"body":"lone \\ud800"}', "body"),
    (raw(note(quote=None)), "quote"),
    (raw(note(quote="")), "quote"),
    (raw(note(quote="q" * 1001)), "quote"),
    (raw(note(claimed_author="a" * 81)), "claimed_author"),
    (raw(note(claimed_author="Ann\u202eevil")), "claimed_author"),
    (raw(note(claimed_author="Ann\u2067")), "claimed_author"),
    (raw(note(claimed_author="Ann\x85")), "claimed_author"),
    (raw(note(claimed_author="Ann\x7f")), "claimed_author"),
    (raw(note(claimed_author="Ann\n")), "claimed_author"),
    (raw(note(claimed_at="2026-02-30T10:15:00Z")), "claimed_at"),
    (raw(note(claimed_at="2026-09-22 10:15:00Z")), "claimed_at"),
    (raw(note(claimed_at="2026-09-22T10:15:00+00:00")), "claimed_at"),
    (raw(note(claimed_at="2026-09-22T10:15:00.1Z")), "claimed_at"),
    (raw(note(claimed_at="\u0662026-09-22T10:15:00Z")), "claimed_at"),
])
def test_parser_rejects_everything_outside_the_closed_schema(data, reason):
    assert reason_of(data) == reason


def test_size_cap_is_on_raw_bytes():
    padded = raw(note(body="x" * 10))
    assert len(padded) < MAX_NOTE_BYTES
    assert reason_of(padded + b" " * (MAX_NOTE_BYTES - len(padded) + 1)) == "too_large"
    parse_note(padded + b" " * (MAX_NOTE_BYTES - len(padded)))


def test_note_sha256_is_stable_across_formatting_and_key_order_only():
    compact = parse_note(json.dumps(note(), separators=(",", ":")).encode())
    pretty = parse_note(json.dumps(dict(reversed(list(note().items()))), indent=4).encode())
    escaped = parse_note(json.dumps(note(body="caf\u00e9"), ensure_ascii=True).encode())
    literal = parse_note(json.dumps(note(body="caf\u00e9"), ensure_ascii=False).encode())
    assert compact.note_sha256 == pretty.note_sha256
    assert escaped.note_sha256 == literal.note_sha256
    assert parse_note(raw(note(body="other"))).note_sha256 != compact.note_sha256
    assert fingerprint_request(compact) == {
        "kind": "portable_feedback_import", "note_sha256": compact.note_sha256,
        "document": "tables/orders.md", "source_sha256": SHA}
    assert import_idempotency_key(compact) == "pfn." + NOTE_ID


def test_marker_carries_only_present_claims_and_no_import_time():
    bare = build_marker(parse_note(raw(note())))
    assert bare == {"schema": 1, "note_id": NOTE_ID, "note_sha256": bare["note_sha256"],
                    "document": "tables/orders.md", "source_sha256": SHA}
    full = build_marker(parse_note(raw(note(quote="q", claimed_author="Ann",
                                             claimed_at="2026-09-22T10:15:00Z"))))
    assert (full["quote"], full["claimed_author"], full["claimed_at"]) == (
        "q", "Ann", "2026-09-22T10:15:00Z")


def test_preview_token_binds_key_session_note_target_and_expiry():
    key = new_preview_key()
    parsed = parse_note(raw(note()))
    token, exp = sign_preview_token(key, csrf_token="session", note=parsed, now=1000)
    assert exp == 1600
    assert verify_preview_token(key, token, csrf_token="session", note=parsed, now=1600)
    assert not verify_preview_token(key, token, csrf_token="session", note=parsed, now=1601)
    assert not verify_preview_token(new_preview_key(), token, csrf_token="session",
                                    note=parsed, now=1000)
    assert not verify_preview_token(key, token, csrf_token="other", note=parsed, now=1000)
    other_body = parse_note(raw(note(body="changed")))
    assert not verify_preview_token(key, token, csrf_token="session", note=other_body, now=1000)
    other_target = parse_note(raw(note(target={"document": "tables/orders.md",
                                               "source_sha256": "cd" * 32})))
    assert not verify_preview_token(key, token, csrf_token="session", note=other_target, now=1000)
    payload, mac = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["exp"] = 10 ** 12
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    for bad in (forged + "." + mac, payload + "." + mac[:-2] + "AA", payload, "",
                None, token + ".x", "!!." + mac, "a" * 2000):
        assert not verify_preview_token(key, bad, csrf_token="session", note=parsed, now=1000)
