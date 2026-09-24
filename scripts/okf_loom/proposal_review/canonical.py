"""Canonical JSON for Proposal / Diff Review v1 (SPEC §3).

All JSON in the protocol is UTF-8 RFC 8259 JSON. Canonical form:

* object keys sorted recursively (code-point order, which equals UTF-8 byte
  order, so no Unicode normalization is implied);
* array order preserved;
* no insignificant whitespace;
* UTF-8 characters emitted directly (``ensure_ascii=False``);
* NaN, infinity, and negative zero rejected;
* duplicate keys rejected at every nesting depth;
* nesting depth (root counted as 1) must not exceed 32.

Booleans are not integers at the schema layer: ``True``/``False`` serialize as
``true``/``false`` and never as ``1``/``0`` (Python's json checks ``bool``
before ``int``); schema validators must guard with ``isinstance(x, bool)``
before treating a value as an integer.

Digest / checksum helpers:

* :func:`canonical_digest`  - ``sha256:`` + lowercase hex SHA-256 over the
  canonical bytes of an object.
* :func:`compute_checksum`  - digest over the canonical form of a mapping with
  its ``checksum`` member removed (SPEC §5.6/§5.7).
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .errors import CanonicalJsonError

#: Maximum JSON nesting depth (SPEC §3: root counts as 1).
MAX_DEPTH: int = 32


def _check_depth(obj: Any, depth: int = 1) -> None:
    if depth > MAX_DEPTH:
        raise CanonicalJsonError(
            f"JSON nesting depth exceeds {MAX_DEPTH} (root counts as 1)",
            path=f"$<depth:{depth}>",
        )
    if isinstance(obj, dict):
        for v in obj.values():
            _check_depth(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _check_depth(v, depth + 1)


def _reject_forbidden_numbers(obj: Any) -> None:
    """Walk ``obj`` rejecting NaN, infinity, and negative-zero floats.

    Booleans are intentionally untouched here (they are valid JSON values and
    are screened for integer misuse at the schema layer).
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise CanonicalJsonError("NaN and infinity are not canonical JSON")
        if obj == 0.0 and math.copysign(1.0, obj) < 0.0:
            raise CanonicalJsonError("negative zero is not canonical JSON")
    elif isinstance(obj, dict):
        for v in obj.values():
            _reject_forbidden_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            _reject_forbidden_numbers(v)


def _reject_dup_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for k, _ in pairs:
        if k in seen:
            raise CanonicalJsonError(f"duplicate object key: {k!r}", path=f"$<key:{k}>")
        seen.add(k)
    return dict(pairs)


def _reject_constants(_value: str) -> float:
    # json emits this hook for the literal tokens NaN / Infinity / -Infinity.
    raise CanonicalJsonError("JSON constant NaN/Infinity is not canonical")


def _parse_float(text: str) -> float:
    f = float(text)
    if f == 0.0 and math.copysign(1.0, f) < 0.0:
        raise CanonicalJsonError("negative zero is not canonical JSON")
    return f


def loads_canonical(text: str | bytes | bytearray) -> Any:
    """Parse JSON rejecting duplicate keys, NaN/inf/-0, and depth > 32.

    Accepts ``str`` or UTF-8 ``bytes``/``bytearray``. Returns native Python
    objects (dict / list / str / int / float / bool / None). Raises
    :class:`~okf_loom.proposal_review.errors.CanonicalJsonError` on any
    canonical-form violation.
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            decoded = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalJsonError(
                f"input is not valid UTF-8: {exc.reason}", path=f"$<byte:{exc.start}>"
            ) from exc
    elif isinstance(text, str):
        decoded = text
    else:
        raise CanonicalJsonError("loads_canonical expects str or bytes")
    try:
        obj = json.loads(
            decoded,
            object_pairs_hook=_reject_dup_pairs,
            parse_constant=_reject_constants,
            parse_float=_parse_float,
        )
    except json.JSONDecodeError as exc:
        raise CanonicalJsonError(
            f"malformed JSON: {exc.msg} at line {exc.lineno} col {exc.colno}",
            path=f"$<line:{exc.lineno}:col:{exc.colno}>",
        ) from exc
    except RecursionError as exc:  # pragma: no cover - defensive
        raise CanonicalJsonError("JSON nesting too deep to parse") from exc
    _reject_forbidden_numbers(obj)
    _check_depth(obj)
    return obj


def dumps_canonical(obj: Any) -> bytes:
    """Serialize canonical JSON as UTF-8 bytes.

    Rejects NaN / infinity / negative zero and depth > 32 before encoding.
    Raises :class:`~okf_loom.proposal_review.errors.CanonicalJsonError` if
    ``obj`` cannot be represented canonically.
    """
    _reject_forbidden_numbers(obj)
    _check_depth(obj)
    try:
        text = json.dumps(
            obj,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalJsonError(f"cannot canonicalize: {exc}") from exc
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def canonical_digest(obj: Any) -> str:
    """``sha256:`` + lowercase hex SHA-256 over the canonical bytes of ``obj``."""
    return "sha256:" + sha256_hex(dumps_canonical(obj))


def compute_checksum(mapping: dict[str, Any], *, field: str = "checksum") -> str:
    """Digest over the canonical form of ``mapping`` with ``field`` removed.

    SPEC §5.6/§5.7: ``checksum`` is the digest of the canonical record
    *excluding* the checksum member. Nested fields are never omitted or
    redacted (SPEC §9 ``repository.json`` rule, applied uniformly here).
    """
    if not isinstance(mapping, dict):
        raise CanonicalJsonError("checksum requires a JSON object")
    stripped = {k: v for k, v in mapping.items() if k != field}
    return canonical_digest(stripped)


__all__ = [
    "MAX_DEPTH",
    "loads_canonical",
    "dumps_canonical",
    "sha256_hex",
    "canonical_digest",
    "compute_checksum",
]
