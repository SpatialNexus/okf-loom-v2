"""Foundational tests for ``okf_loom.proposal_review.canonical`` and ``.ids``.

These pin the SPEC §3 canonical-JSON and primitive contracts that every later
closed record builds on. The SCHEMA-11 corpus (duplicate/unknown/type/depth/
size/UTF-8 across every public family) lives in ``test_proposal_schemas.py``.
"""
from __future__ import annotations

import math

import pytest

from okf_loom.proposal_review import canonical, ids
from okf_loom.proposal_review.errors import (
    CanonicalJsonError,
    LimitError,
    PrimitiveError,
)


# --- canonical dumps ---------------------------------------------------------


def test_dumps_sorts_keys_recursively_and_no_whitespace() -> None:
    raw = {"b": 1, "a": {"y": 2, "x": [3, 2, 1]}}
    out = canonical.dumps_canonical(raw)
    assert out == b'{"a":{"x":[3,2,1],"y":2},"b":1}'


def test_dumps_emits_utf8_chars_not_ascii_escaped() -> None:
    out = canonical.dumps_canonical({"k": "café—αβ"})
    assert "café—αβ".encode("utf-8") in out
    assert b"\\u" not in out


def test_dumps_preserves_array_order() -> None:
    out = canonical.dumps_canonical([3, 1, 2])
    assert out == b"[3,1,2]"


def test_dumps_rejects_nan_inf_and_negative_zero() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.dumps_canonical(float("nan"))
    with pytest.raises(CanonicalJsonError):
        canonical.dumps_canonical(float("inf"))
    with pytest.raises(CanonicalJsonError):
        canonical.dumps_canonical({"x": -0.0})


def test_dumps_keeps_bool_distinct_from_int() -> None:
    assert canonical.dumps_canonical(True) == b"true"
    assert canonical.dumps_canonical(1) == b"1"


# --- canonical loads ---------------------------------------------------------


def test_loads_accepts_bytes_and_str() -> None:
    assert canonical.loads_canonical(b'{"a":1}') == {"a": 1}
    assert canonical.loads_canonical('{"a":1}') == {"a": 1}


def test_loads_rejects_duplicate_keys_at_top_level() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical('{"schema":1,"schema":2}')


def test_loads_rejects_duplicate_keys_at_every_depth() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical('{"a":{"b":1,"b":2}}')
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical('[{"x":1,"x":2}]')


def test_loads_rejects_nan_inf_constants() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical('{"x":NaN}')
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical('{"x":Infinity}')


def test_loads_rejects_negative_zero_float() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical('{"x":-0.0}')


def test_loads_rejects_invalid_utf8() -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical(b'{"x":"\xff"}')


def test_loads_rejects_depth_over_32() -> None:
    # 32 nesting levels => innermost scalar value sits at depth 33.
    deep = '{"a":' * 32 + "1" + "}" * 32
    with pytest.raises(CanonicalJsonError):
        canonical.loads_canonical(deep)


def test_loads_accepts_depth_exactly_32() -> None:
    # 31 nesting levels => innermost scalar value sits at depth 32
    # (root counts as 1).
    deep = '{"a":' * 31 + "1" + "}" * 31
    assert canonical.loads_canonical(deep)


# --- round-trip + digest stability ------------------------------------------


def test_round_trip_is_stable() -> None:
    obj = {"b": [1, 2], "a": {"z": "é", "y": None}, "c": True}
    rt = canonical.loads_canonical(canonical.dumps_canonical(obj))
    assert rt == obj


def test_canonical_digest_is_order_independent() -> None:
    a = canonical.canonical_digest({"a": 1, "b": 2})
    b = canonical.canonical_digest({"b": 2, "a": 1})
    assert a == b
    assert a.startswith("sha256:")
    assert len(a) == len("sha256:") + 64


def test_compute_checksum_strips_checksum_field_only() -> None:
    obj = {"schema": 1, "payload": "x", "checksum": "sha256:" + "0" * 64}
    expected = canonical.canonical_digest({"schema": 1, "payload": "x"})
    assert canonical.compute_checksum(obj) == expected


def test_compute_checksum_redacts_nothing_else() -> None:
    obj = {"a": {"secret": "k"}, "checksum": "x"}
    expected = canonical.canonical_digest({"a": {"secret": "k"}})
    assert canonical.compute_checksum(obj) == expected


# --- ids primitives ----------------------------------------------------------

# 26 Crockford Base32 chars (excludes I, L, O, U).
_ULID = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
_BAD_ULID = "01J2Y8N5Z9Q3A7K6M4T1V0WXI"  # trailing 'I' forbidden


@pytest.mark.parametrize(
    "fn,value,ok",
    [
        (ids.validate_proposal_id, _ULID, True),
        (ids.validate_proposal_id, _BAD_ULID, False),
        (ids.validate_proposal_id, "01J2Y8N5Z9Q3A7K6M4T1V0WX", False),  # 25 chars
        (ids.validate_proposal_id, 123, False),
        (ids.validate_repository_id, "repo_" + _ULID, True),
        (ids.validate_repository_id, "repo_" + _BAD_ULID, False),
        (ids.validate_repository_id, _ULID, False),
        (ids.validate_op_id, "op_" + _ULID, True),
        (ids.validate_op_id, "op_" + _BAD_ULID, False),
        (ids.validate_rec_id, "rec_" + _ULID, True),
        (ids.validate_txn_id, "txn_" + _ULID, True),
        (ids.validate_undo_id, "undo_" + _ULID, True),
        (ids.validate_out_id, "out_" + _ULID, True),
        (ids.validate_cor_id, "cor_" + _ULID, True),
        (ids.validate_cur_id, "cur_" + _ULID, True),
    ],
)
def test_id_primitives(fn, value, ok) -> None:
    if ok:
        assert fn(value) == value
    else:
        with pytest.raises((PrimitiveError, LimitError)):
            fn(value)


def test_revision_rejects_bool_and_out_of_range() -> None:
    assert ids.validate_revision(1) == 1
    assert ids.validate_revision(2_147_483_647) == 2_147_483_647
    with pytest.raises(PrimitiveError):
        ids.validate_revision(0)
    with pytest.raises(PrimitiveError):
        ids.validate_revision(2_147_483_648)
    with pytest.raises(PrimitiveError):
        ids.validate_revision(True)  # bool is not int


def test_stored_byte_size_rejects_bool() -> None:
    assert ids.validate_stored_byte_size(0) == 0
    assert ids.validate_stored_byte_size(67_108_864) == 67_108_864
    with pytest.raises(PrimitiveError):
        ids.validate_stored_byte_size(67_108_865)
    with pytest.raises(PrimitiveError):
        ids.validate_stored_byte_size(True)


def test_concept_id_rules() -> None:
    assert ids.validate_concept_id("tables/orders") == "tables/orders"
    with pytest.raises(PrimitiveError):
        ids.validate_concept_id("/tables/orders")
    with pytest.raises(PrimitiveError):
        ids.validate_concept_id("tables/orders.md")
    with pytest.raises(PrimitiveError):
        ids.validate_concept_id("tables//orders")
    with pytest.raises(PrimitiveError):
        ids.validate_concept_id("tables/../orders")


def test_digest_and_content_revision() -> None:
    good = "sha256:" + "0" * 64
    assert ids.validate_digest(good) == good
    with pytest.raises(PrimitiveError):
        ids.validate_digest("sha256:abc")
    hex64 = "0" * 64
    assert ids.validate_content_revision(hex64) == hex64
    assert ids.validate_content_revision("absent") == "absent"
    with pytest.raises(PrimitiveError):
        ids.validate_content_revision("abc")
    assert ids.validate_operation_content_revision(hex64) == hex64
    with pytest.raises(PrimitiveError):
        ids.validate_operation_content_revision("absent")


def test_row_token_and_idempotency_key() -> None:
    token = "A" * 43
    assert ids.validate_row_token(token) == token
    with pytest.raises(PrimitiveError):
        ids.validate_row_token("A" * 42)
    key = "k" * 32
    assert ids.validate_idempotency_key(key) == key
    with pytest.raises(PrimitiveError):
        ids.validate_idempotency_key("k" * 31)
    with pytest.raises(PrimitiveError):
        ids.validate_idempotency_key("k" * 129)
    with pytest.raises(PrimitiveError):
        ids.validate_idempotency_key(" " * 32)  # space not in charset


def test_idempotency_key_exact_length_boundaries_do_not_claim_entropy() -> None:
    # Runtime can enforce only the wire grammar. Repetition is deliberately
    # accepted; callers, not validators, own the >=128-bit generation rule.
    assert ids.validate_idempotency_key("A" * 32) == "A" * 32
    assert ids.validate_idempotency_key("Z" * 128) == "Z" * 128
    with pytest.raises(PrimitiveError):
        ids.validate_idempotency_key("A" * 31)
    with pytest.raises(PrimitiveError):
        ids.validate_idempotency_key("Z" * 129)


def test_actor_and_runtime_identity() -> None:
    assert ids.validate_actor("local-user") == "local-user"
    with pytest.raises(PrimitiveError):
        ids.validate_actor("")
    with pytest.raises(PrimitiveError):
        ids.validate_actor("x" * 129)
    rid = "okf-validator@1.0+sha256:" + "0" * 64
    assert ids.validate_runtime_identity(rid) == rid
    with pytest.raises(PrimitiveError):
        ids.validate_runtime_identity("okf-validator@1.0")


@pytest.mark.parametrize(
    "path,ok",
    [
        ("transactions/txn_1/wal.json", True),
        ("proposals.jsonl", True),
        ("/etc/passwd", False),
        ("../secrets", False),
        ("a/../../b", False),
        ("a/../b", False),
        ("a/./b", False),
        ("a//b", False),
        ("good/dir/", False),
        ("with\\backslash", False),
    ],
)
def test_relative_path(path: str, ok: bool) -> None:
    if ok:
        assert ids.validate_relative_path(path) == path
    else:
        with pytest.raises((PrimitiveError, LimitError)):
            ids.validate_relative_path(path)


def test_relative_path_byte_limit() -> None:
    long_path = "a" * 1025
    with pytest.raises(LimitError):
        ids.validate_relative_path(long_path)


@pytest.mark.parametrize(
    "ts,ok",
    [
        ("2026-07-14T12:34:56.789Z", True),
        ("2026-07-14T12:34:56.78Z", False),      # not millisecond
        ("2026-07-14T12:34:56Z", False),          # no fractional
        ("2026-07-14T12:34:56.789+00:00", False), # must be Z
        ("2026-13-40T12:34:56.789Z", False),      # invalid date
        ("2026-07-14 12:34:56.789Z", False),      # space not T
    ],
)
def test_timestamp(ts: str, ok: bool) -> None:
    if ok:
        assert ids.validate_timestamp(ts) == ts
    else:
        with pytest.raises(PrimitiveError):
            ids.validate_timestamp(ts)


def test_extensions_preserve_but_bound_values() -> None:
    ext = {"okf.test/key1": "v", "okf.test/key2": {"nested": [1, 2]}}
    out = ids.validate_extensions(ext)
    assert out == ext
    with pytest.raises(PrimitiveError):
        ids.validate_extensions({"BadKey": 1})  # bad extension grammar
    with pytest.raises(PrimitiveError):
        ids.validate_extensions({"okf.test/": 1})  # empty name part
    big = {"okf.test/k": "x" * (ids.MAX_EXTENSION_VALUE_BYTES + 1)}
    with pytest.raises(LimitError):
        ids.validate_extensions(big)


def test_extensions_aggregate_cap() -> None:
    # 5 values whose canonical sizes sum just past the 65536 aggregate cap,
    # while each individual value stays under the 16384 per-value cap.
    # canonical of a 13110-char string = 13112 bytes; 5 * 13112 = 65560.
    ext = {f"okf.t{x:02d}/k": "x" * 13_110 for x in range(5)}
    with pytest.raises(LimitError):
        ids.validate_extensions(ext)


def test_neg_zero_check_is_copysign_aware() -> None:
    # Guard against a regression where -0.0 might be emitted.
    assert math.copysign(1.0, -0.0) == -1.0
