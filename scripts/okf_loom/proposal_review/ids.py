"""Primitive id / path / grammar validators for Proposal / Diff Review v1.

Implements every SPEC §3 primitive and the §4 length / size bounds that apply
to a single primitive. Every validator either returns the canonical string
form of a valid value or raises
:class:`~okf_loom.proposal_review.errors.PrimitiveError` (or
:class:`~okf_loom.proposal_review.errors.LimitError` for bound faults).

All validators are pure and side-effect-free.
"""
from __future__ import annotations

import datetime
import re
from typing import Any

from .canonical import dumps_canonical
from .errors import CanonicalJsonError, LimitError, PrimitiveError

# --- alphabets / regexes (SPEC §3) -------------------------------------------

# 26 uppercase Crockford Base32 chars; Crockford excludes I, L, O, U.
_CROCKFORD_26 = re.compile(r"\A[0-9A-HJKMNP-TV-Z]{26}\Z")

_DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_BASE64URL_43 = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")
_IDEMPOTENCY_KEY = re.compile(r"\A[A-Za-z0-9._~-]{32,128}\Z")
_EXTENSION_KEY = re.compile(r"\A[a-z0-9][a-z0-9.-]{0,62}/[A-Za-z0-9_.-]{1,64}\Z")
_RELATION = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_FRONTMATTER_KEY = re.compile(r"\A[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_CONCEPT_SEGMENT = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_RELATIVE_PATH = re.compile(
    r"\A(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z"
)
_RUNTIME_IDENTITY = re.compile(
    r"\A[A-Za-z][A-Za-z0-9._-]*@[0-9]+\.[0-9]+\+sha256:[0-9a-f]{64}\Z"
)
_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")

# Kind prefixes for record ids (SPEC §3 "Record id").
RECORD_ID_PREFIXES: frozenset[str] = frozenset(
    {"rec_", "txn_", "undo_", "out_", "cor_", "svc_", "act_"}
)

# Cursor key id prefix (SPEC §9 ``cursor_keys``).
CURSOR_ID_PREFIX: str = "cur_"

# The absent sentinel for content revisions.
ABSENT: str = "absent"

# --- SPEC §4 primitive bounds ------------------------------------------------

MAX_REVISION: int = 2_147_483_647
MAX_STORED_BYTE_SIZE: int = 67_108_864
MAX_EXTENSION_VALUE_BYTES: int = 16_384
MAX_EXTENSION_AGGREGATE_BYTES: int = 65_536
MAX_RELATIVE_PATH_BYTES: int = 1_024
MAX_ACTOR_CODEPOINTS: int = 128
MAX_IDEMPOTENCY_KEY_LEN: int = 128
MIN_IDEMPOTENCY_KEY_LEN: int = 32


def _need_str(v: Any, name: str) -> str:
    if not isinstance(v, str):
        raise PrimitiveError(f"{name} must be a string", path=name)
    return v


# --- id primitives -----------------------------------------------------------


def validate_proposal_id(v: Any) -> str:
    """26 uppercase Crockford Base32 ULID characters (excludes I,L,O,U)."""
    s = _need_str(v, "proposal_id")
    if not _CROCKFORD_26.fullmatch(s):
        raise PrimitiveError(
            "proposal_id must be 26 Crockford Base32 chars (excl. I,L,O,U)",
            path="proposal_id",
        )
    return s


def _prefixed_id(v: Any, prefix: str, label: str) -> str:
    s = _need_str(v, label)
    if not s.startswith(prefix):
        raise PrimitiveError(f"{label} must start with {prefix!r}", path=label)
    body = s[len(prefix):]
    if not _CROCKFORD_26.fullmatch(body):
        raise PrimitiveError(
            f"{label} body must be 26 Crockford Base32 chars (excl. I,L,O,U)",
            path=label,
        )
    return s


def validate_repository_id(v: Any) -> str:
    """``repo_`` + 26 Crockford Base32 chars."""
    return _prefixed_id(v, "repo_", "repository_id")


def validate_op_id(v: Any) -> str:
    """``op_`` + 26 Crockford Base32 chars."""
    return _prefixed_id(v, "op_", "op_id")


def validate_record_id(v: Any, prefix: str) -> str:
    """Closed kind-prefixed id, including caller-assigned ``act_`` activity ids."""
    if prefix not in RECORD_ID_PREFIXES:
        raise PrimitiveError(f"unsupported record-id prefix: {prefix!r}")
    return _prefixed_id(v, prefix, f"{prefix}id")


def validate_rec_id(v: Any) -> str:
    return _prefixed_id(v, "rec_", "record_id")


def validate_txn_id(v: Any) -> str:
    return _prefixed_id(v, "txn_", "transaction_id")


def validate_undo_id(v: Any) -> str:
    return _prefixed_id(v, "undo_", "undo_transaction_id")


def validate_transaction_id_any(v: Any) -> str:
    """Validate a transaction id that may be ``txn_`` or ``undo_`` prefixed.

    Used by schemas whose ``transaction_id`` field can reference either an
    apply (``txn_``) or undo (``undo_``) transaction (endpoint results,
    tombstones, notifications).
    """
    s = _need_str(v, "transaction_id")
    if s.startswith("undo_"):
        return _prefixed_id(s, "undo_", "undo_transaction_id")
    return _prefixed_id(s, "txn_", "transaction_id")


def validate_out_id(v: Any) -> str:
    return _prefixed_id(v, "out_", "outbox_id")


def validate_svc_id(v: Any) -> str:
    return _prefixed_id(v, "svc_", "intent_id")


def validate_cor_id(v: Any) -> str:
    return _prefixed_id(v, "cor_", "corruption_id")


def validate_cur_id(v: Any) -> str:
    return _prefixed_id(v, CURSOR_ID_PREFIX, "cursor_key_id")


# --- scalar primitives -------------------------------------------------------


def validate_revision(v: Any, *, name: str = "revision") -> int:
    """JSON integer in ``1..2147483647`` (booleans are not integers)."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise PrimitiveError(f"{name} must be a JSON integer", path=name)
    if isinstance(v, int) and (v < 1 or v > MAX_REVISION):
        raise PrimitiveError(f"{name} must be in 1..{MAX_REVISION}", path=name)
    return v


def validate_stored_byte_size(
    v: Any, *, limit: int = MAX_STORED_BYTE_SIZE, name: str = "size"
) -> int:
    """JSON integer in ``0..67108864``; never a boolean."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise PrimitiveError(f"{name} must be a JSON integer", path=name)
    if v < 0 or v > limit:
        raise PrimitiveError(f"{name} must be in 0..{limit}", path=name)
    return v


def validate_concept_id(v: Any, *, name: str = "concept") -> str:
    """Canonical bundle-relative concept id (no leading slash, no ``.md``)."""
    s = _need_str(v, name)
    if s.endswith(".md"):
        raise PrimitiveError(f"{name} must not end with .md", path=name)
    if s.startswith("/") or s.endswith("/"):
        raise PrimitiveError(
            f"{name} must not have a leading or trailing slash", path=name
        )
    if "//" in s:
        raise PrimitiveError(f"{name} must not contain empty segments", path=name)
    segments = s.split("/")
    if not segments or any(not seg for seg in segments):
        raise PrimitiveError(f"{name} must be non-empty", path=name)
    for seg in segments:
        if not _CONCEPT_SEGMENT.fullmatch(seg):
            raise PrimitiveError(
                f"{name} segment {seg!r} is not canonical", path=name
            )
    return s


def validate_digest(v: Any, *, name: str = "digest") -> str:
    """``sha256:`` + 64 lowercase hex."""
    s = _need_str(v, name)
    if not _DIGEST.fullmatch(s):
        raise PrimitiveError(
            f"{name} must be 'sha256:' + 64 lowercase hex", path=name
        )
    return s


def validate_content_revision(v: Any, *, name: str = "content_revision") -> str:
    """64 lowercase hex SHA-256, or the literal ``absent`` sentinel."""
    s = _need_str(v, name)
    if s == ABSENT:
        return s
    if not _HEX64.fullmatch(s):
        raise PrimitiveError(
            f"{name} must be 64 lowercase hex or the literal 'absent'",
            path=name,
        )
    return s


def validate_operation_content_revision(v: Any, *, name: str = "expected_revision") -> str:
    """64 lowercase hex SHA-256 only (``absent`` is invalid inside an operation)."""
    s = _need_str(v, name)
    if s == ABSENT:
        raise PrimitiveError(
            "absent is not a valid operation expected revision in v1", path=name
        )
    if not _HEX64.fullmatch(s):
        raise PrimitiveError(
            f"{name} must be 64 lowercase hex", path=name
        )
    return s


def validate_row_token(v: Any, *, name: str = "row_token") -> str:
    """43-character unpadded base64url encoding of 32 random bytes."""
    s = _need_str(v, name)
    if not _BASE64URL_43.fullmatch(s):
        raise PrimitiveError(
            f"{name} must be 43 unpadded base64url chars", path=name
        )
    return s


def validate_idempotency_key(v: Any, *, name: str = "idempotency_key") -> str:
    """32-128 visible ASCII chars matching ``[A-Za-z0-9._~-]``.

    The "at least 128 bits of caller-generated entropy" requirement is a
    runtime/server-side caller claim that cannot be verified from a single
    key structurally; the schema layer enforces length + charset only.
    """
    s = _need_str(v, name)
    if not _IDEMPOTENCY_KEY.fullmatch(s):
        raise PrimitiveError(
            f"{name} must be 32-128 visible ASCII chars matching [A-Za-z0-9._~-]",
            path=name,
        )
    return s


def validate_actor(v: Any, *, name: str = "actor") -> str:
    """Non-empty UTF-8 string of 1-128 code points."""
    s = _need_str(v, name)
    if not s:
        raise PrimitiveError(f"{name} must be non-empty", path=name)
    if len(s) > MAX_ACTOR_CODEPOINTS:
        raise PrimitiveError(
            f"{name} must be 1..{MAX_ACTOR_CODEPOINTS} code points", path=name
        )
    return s


def validate_runtime_identity(v: Any, *, name: str = "runtime_identity") -> str:
    """``name@major.minor+sha256:<64-lower-hex>``."""
    s = _need_str(v, name)
    if not _RUNTIME_IDENTITY.fullmatch(s):
        raise PrimitiveError(
            f"{name} must match name@major.minor+sha256:<64hex>", path=name
        )
    return s


def validate_relation(v: Any) -> str | None:
    """``null`` or a relation matching ``^[a-z][a-z0-9_]{0,63}$``."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise PrimitiveError("relation must be null or a string", path="relation")
    if not _RELATION.fullmatch(v):
        raise PrimitiveError(
            "relation must match ^[a-z][a-z0-9_]{0,63}$", path="relation"
        )
    return v


def validate_frontmatter_key(v: Any) -> str:
    """Frontmatter key ``^[A-Za-z_][A-Za-z0-9_-]{0,63}$`` that is not ``type``."""
    s = _need_str(v, "frontmatter key")
    if not _FRONTMATTER_KEY.fullmatch(s):
        raise PrimitiveError(
            "frontmatter key must match ^[A-Za-z_][A-Za-z0-9_-]{0,63}$",
            path="key",
        )
    if s == "type":
        raise PrimitiveError(
            "set_frontmatter cannot modify 'type'", path="key"
        )
    return s


def validate_relative_path(v: Any, *, name: str = "relative_path") -> str:
    """UTF-8 string of 1-1024 bytes matching the canonical repository path regex."""
    s = _need_str(v, name)
    if len(s.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise LimitError(
            f"{name} must be 1..{MAX_RELATIVE_PATH_BYTES} UTF-8 bytes", path=name
        )
    if "\x00" in s or "\\" in s:
        raise PrimitiveError(f"{name} must not contain NUL or backslash", path=name)
    if not _RELATIVE_PATH.fullmatch(s):
        raise PrimitiveError(
            f"{name} must be a canonical repository-relative path", path=name
        )
    return s


def validate_timestamp(v: Any, *, name: str = "timestamp") -> str:
    """UTC RFC 3339 with exactly millisecond precision, e.g. ``2026-07-14T12:34:56.789Z``."""
    s = _need_str(v, name)
    if not _TIMESTAMP.fullmatch(s):
        raise PrimitiveError(
            f"{name} must be UTC RFC3339 with millisecond precision "
            "(e.g. 2026-07-14T12:34:56.789Z)",
            path=name,
        )
    try:
        datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise PrimitiveError(
            f"{name} is not a valid UTC instant: {exc}", path=name
        ) from exc
    return s


# --- extensions --------------------------------------------------------------


def validate_extension_key(v: Any) -> str:
    s = _need_str(v, "extension key")
    if not _EXTENSION_KEY.fullmatch(s):
        raise PrimitiveError(
            "extension key must match ^[a-z0-9][a-z0-9.-]{0,62}/[A-Za-z0-9_.-]{1,64}$",
            path="extension_key",
        )
    return s


def validate_extensions(v: Any) -> dict[str, Any]:
    """Validate a namespaced extension object (SPEC §3).

    Keys must match the extension grammar; each value must canonicalize to
    <= 16384 bytes; the aggregate must be <= 65536 canonical bytes. Values
    are preserved verbatim - core readers derive no authority from them.
    """
    if not isinstance(v, dict):
        raise PrimitiveError("extensions must be an object", path="extensions")
    aggregate = 0
    for k, val in v.items():
        validate_extension_key(k)
        try:
            payload = dumps_canonical(val)
        except CanonicalJsonError as exc:
            raise PrimitiveError(
                f"extension value for {k!r} is not canonical JSON: {exc.message}",
                path=f"extensions.{k}",
            ) from exc
        if len(payload) > MAX_EXTENSION_VALUE_BYTES:
            raise LimitError(
                f"extension value for {k!r} exceeds "
                f"{MAX_EXTENSION_VALUE_BYTES} canonical bytes",
                path=f"extensions.{k}",
            )
        aggregate += len(payload)
    if aggregate > MAX_EXTENSION_AGGREGATE_BYTES:
        raise LimitError(
            f"extensions aggregate exceeds "
            f"{MAX_EXTENSION_AGGREGATE_BYTES} canonical bytes",
            path="extensions",
        )
    return dict(v)


__all__ = [
    "RECORD_ID_PREFIXES",
    "CURSOR_ID_PREFIX",
    "ABSENT",
    "MAX_REVISION",
    "MAX_STORED_BYTE_SIZE",
    "MAX_EXTENSION_VALUE_BYTES",
    "MAX_EXTENSION_AGGREGATE_BYTES",
    "MAX_RELATIVE_PATH_BYTES",
    "MAX_ACTOR_CODEPOINTS",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "MIN_IDEMPOTENCY_KEY_LEN",
    "validate_proposal_id",
    "validate_repository_id",
    "validate_op_id",
    "validate_record_id",
    "validate_rec_id",
    "validate_txn_id",
    "validate_undo_id",
    "validate_transaction_id_any",
    "validate_out_id",
    "validate_svc_id",
    "validate_cor_id",
    "validate_cur_id",
    "validate_revision",
    "validate_stored_byte_size",
    "validate_concept_id",
    "validate_digest",
    "validate_content_revision",
    "validate_operation_content_revision",
    "validate_row_token",
    "validate_idempotency_key",
    "validate_actor",
    "validate_runtime_identity",
    "validate_relation",
    "validate_frontmatter_key",
    "validate_relative_path",
    "validate_timestamp",
    "validate_extension_key",
    "validate_extensions",
]
