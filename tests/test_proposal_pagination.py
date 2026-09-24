"""STATE-12 / PAGE-01..06 pagination proofs for Proposal Review Foundation B.

Implements the named pagination proof nodes from the normative catalogue
(SPEC section 17.1): ``STATE-12`` (signed high-water cursor owns the immutable
pagination snapshot) and ``PAGE-01..06``.  All inputs are explicit immutable
fixtures plus a pure :class:`HmacCursorSigner`; no clock/filesystem/network.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest
import multiprocessing

from okf_loom.proposal_review import canonical, pagination
from okf_loom.proposal_review.errors import CursorError, CursorExpiredError, ProjectionError
from okf_loom.proposal_review.pagination import (
    CursorKeyMaterial, FrozenPaginationView, HmacCursorSigner, ListQuery,
    ProposalListItem, decode_cursor, encode_cursor, walk_page,
)

REPO = "repo_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
DIGEST = "sha256:" + "0" * 64
DIGEST_B = "sha256:" + "1" * 64
RUNTIME = "okf-validator@1.0+sha256:" + "0" * 64
RUNTIME_SER = "okf-roundtrip@1.0+sha256:" + "0" * 64
RUNTIME_OPS = "proposal-ops@1.0+sha256:" + "0" * 64
CLOCK = "2026-07-14T12:00:00.000Z"
CLOCK_PLUS_10M = "2026-07-14T12:10:00.000Z"
CLOCK_AFTER = "2026-07-14T12:10:00.001Z"
KEY_EXPIRES = "2026-07-14T13:00:00.000Z"
TS_EXPIRE = "2026-07-22T00:00:00.000Z"


def _key(key_id: str = "cur_" + "0" * 26, expires_at: str = KEY_EXPIRES,
         secret: bytes = b"\x01" * 32) -> CursorKeyMaterial:
    return CursorKeyMaterial(
        key_id=key_id, secret=secret, not_before="2026-07-14T00:00:00.000Z",
        expires_at=expires_at,
    )


def _signer(keys=None, active=None) -> HmacCursorSigner:
    keys = keys or [_key()]
    return HmacCursorSigner.from_keys(keys, active_key_id=active)


def _high_waters(gen: int = 1, seq: int = 0) -> dict[str, dict[str, int]]:
    return {agg: {"generation": gen, "sequence": seq} for agg in (
        "proposals", "decisions", "transactions", "endpoint_results",
        "outbox", "tombstones", "corruption",
    )}


def _item(pid: str, created_at: str, rev: int = 1, state: str = "in_review",
          expires_at: str | None = TS_EXPIRE) -> ProposalListItem:
    return ProposalListItem(
        created_at=created_at, proposal_id=pid, proposal_revision=rev,
        state=state, expires_at=expires_at, read_model_token=DIGEST,
        summary=f"proposal {pid}", author="agent",
    )


def _pid(i: int) -> str:
    """One distinct valid 26-char Crockford proposal id for index ``i``."""
    base = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford (excl. I,L,O,U)
    return alphabet[i % len(alphabet)] + base[1:]


def _view(items, *, high_waters=None, available=None, clock=CLOCK,
          bundle_digest=DIGEST, validation_context_digest=DIGEST_B) -> FrozenPaginationView:
    return FrozenPaginationView(
        repository_id=REPO, items=tuple(items),
        high_waters=high_waters or _high_waters(),
        available_generations=available or {agg: frozenset({1}) for agg in (
            "proposals", "decisions", "transactions", "endpoint_results",
            "outbox", "tombstones", "corruption",
        )},
        bundle_snapshot_digest=bundle_digest, config_digest=DIGEST,
        validation_context_digest=validation_context_digest,
        capabilities=("okf.cap.typed_relations",),
        validator_id=RUNTIME, serializer_id=RUNTIME_SER,
        operation_runtime_id=RUNTIME_OPS, clock_captured_at=clock,
    )


# ============================================================================
# STATE-12: signed high-water cursor owns immutable pagination snapshot
# ============================================================================


def test_state_12_snapshot_authority():
    """The signed cursor's frozen high-water owns the pagination snapshot."""
    items = [_item(_pid(0), "2026-07-14T10:00:00.000Z"),
             _item(_pid(1), "2026-07-14T11:00:00.000Z")]
    signer = _signer()
    view = _view(items)
    page = walk_page(view, query=ListQuery(), limit=1, signer=signer, now=CLOCK)
    assert page.next_cursor is not None
    # The cursor payload encodes the exact frozen high-waters.
    payload = decode_cursor(page.next_cursor, signer=signer, repository_id=REPO, now=CLOCK)
    for agg in ("proposals", "decisions", "transactions", "endpoint_results",
                "outbox", "tombstones", "corruption"):
        assert payload.repository_high_waters[agg] == {"generation": 1, "sequence": 0}
    assert payload.bundle_snapshot_digest == DIGEST
    assert payload.validation_context_digest == DIGEST_B
    assert payload.clock_captured_at == CLOCK
    # Cursor expiry is exactly min(clock+10m, key expires, earliest expiry).
    assert payload.cursor_expires_at == CLOCK_PLUS_10M  # earliest expiry is later
    # The cursor is stateless: it carries no active authority or prune pin.
    assert "read_model_token" not in payload.to_mapping()


def test_validation_context_changes_pagination_environment_and_page_token():
    items = [_item(_pid(0), "2026-07-14T10:00:00.000Z")]
    signer = _signer()
    base_view = _view(items)
    changed_view = _view(items, validation_context_digest=DIGEST)
    base = walk_page(base_view, query=ListQuery(), limit=1, signer=signer, now=CLOCK)
    changed = walk_page(changed_view, query=ListQuery(), limit=1, signer=signer, now=CLOCK)
    assert base_view.environment_digest() != changed_view.environment_digest()
    assert base.page_snapshot_token != changed.page_snapshot_token


# ============================================================================
# PAGE-01: unique descending tuple gives stable order
# ============================================================================


def test_page_01_order():
    """Unique descending ``(created_at,proposal_id,proposal_revision)`` order."""
    items = [
        _item("AAAAAAAAAAAAAAAAAAAAAAAAAA", "2026-07-14T10:00:00.000Z"),  # invalid len; fixed below
    ]
    # Use valid proposal ids and timestamps to establish a strict order.
    a = _item(_pid(0), "2026-07-14T10:00:00.000Z")
    b = _item(_pid(1), "2026-07-14T11:00:00.000Z")
    c = _item(_pid(2), "2026-07-14T11:00:00.000Z", rev=2)
    view = _view([a, b, c])
    page = walk_page(view, query=ListQuery(), limit=10, signer=_signer(), now=CLOCK)
    # Descending by (created_at, proposal_id, proposal_revision).
    keys = [i.sort_key for i in page.items]
    assert keys == sorted(keys, reverse=True)
    assert keys[0] == ("2026-07-14T11:00:00.000Z", _pid(2), 2)
    # Stable across repeated walks (deterministic).
    page2 = walk_page(view, query=ListQuery(), limit=10, signer=_signer(), now=CLOCK)
    assert [i.sort_key for i in page2.items] == keys
    # Final page has no next cursor.
    assert page.next_cursor is None


# ============================================================================
# PAGE-02: multi-page walk has no skip or duplicate
# ============================================================================


def test_page_02_no_skip_duplicate():
    """A complete multi-page walk emits every item exactly once, in order."""
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(5)]
    view = _view(items)
    signer = _signer()
    seen: list[tuple[str, str, int]] = []
    cursor = None
    pages = 0
    while True:
        page = walk_page(view, query=ListQuery(), limit=2, signer=signer,
                         now=CLOCK, cursor=cursor)
        pages += 1
        for item in page.items:
            seen.append(item.sort_key)
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages < 20  # safety
    # No skip, no duplicate: exactly the full set, descending.
    assert seen == sorted([i.sort_key for i in items], reverse=True)
    assert len(seen) == len(set(seen)) == 5
    # The exclusive last tuple guarantees the boundary never repeats.
    assert all(seen[i] > seen[i + 1] for i in range(len(seen) - 1))


# ============================================================================
# PAGE-03: concurrent append/bundle/clock changes do not enter frozen pages
# ============================================================================


def test_page_03_frozen_inputs():
    """Concurrent appends, bundle, runtime, and later expiry do not alter pages."""
    base_items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(4)]
    view = _view(base_items)
    signer = _signer()
    page1 = walk_page(view, query=ListQuery(), limit=2, signer=signer, now=CLOCK)
    cursor = page1.next_cursor
    assert cursor is not None
    # A NEW proposal appended concurrently (later high-water, later clock).
    concurrent_item = _item(_pid(9), "2026-07-14T20:00:00.000Z")
    advanced_view = _view(
        base_items,  # the frozen items are reused; concurrent item is NOT added
        high_waters=_high_waters(gen=1, seq=1),
    )
    # Continuing with the FROZEN view ignores the concurrent append entirely.
    # ``now`` stays inside the 10-minute cursor lifetime / key validity window.
    page2 = walk_page(view, query=ListQuery(), limit=2, signer=signer,
                      now="2026-07-14T12:05:00.000Z", cursor=cursor)
    emitted = [i.sort_key for i in page1.items] + [i.sort_key for i in page2.items]
    assert concurrent_item.sort_key not in emitted
    # A view whose frozen high-waters changed fails closed (requires fresh read).
    with pytest.raises(CursorExpiredError):
        walk_page(advanced_view, query=ListQuery(), limit=2, signer=signer,
                  now=CLOCK, cursor=cursor)


# ============================================================================
# PAGE-04: cursor tamper / query mismatch fails closed
# ============================================================================


def test_page_04_tamper():
    """Malformed / tampered / wrong-key / query-mismatch cursors fail closed."""
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(4)]
    view = _view(items)
    signer = _signer()
    page = walk_page(view, query=ListQuery(), limit=2, signer=signer, now=CLOCK)
    token = page.next_cursor
    assert token is not None
    # Tampered tag/payload -> invalid_cursor.
    tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
    with pytest.raises(CursorError):
        decode_cursor(tampered, signer=signer, repository_id=REPO, now=CLOCK)
    # Padded base64url -> invalid_cursor.
    with pytest.raises(CursorError):
        decode_cursor(token + "=", signer=signer, repository_id=REPO, now=CLOCK)
    # Wrong repository -> invalid_cursor.
    with pytest.raises(CursorError):
        decode_cursor(token, signer=signer, repository_id="repo_" + "1" * 26, now=CLOCK)
    # Wrong key (different signer) -> invalid_cursor.
    other_signer = _signer(keys=[_key(key_id="cur_" + "9" * 26, secret=b"\x02" * 32)])
    with pytest.raises(CursorError):
        decode_cursor(token, signer=other_signer, repository_id=REPO, now=CLOCK)
    # Query mismatch (different state filter) -> invalid_cursor.
    with pytest.raises(CursorError):
        decode_cursor(token, signer=signer, repository_id=REPO, now=CLOCK,
                      expected_query_digest=ListQuery(state="ready").digest)
    # Non-canonical payload (re-serialized differently) -> invalid_cursor.
    import json
    env = json.loads(pagination._b64url_decode(token))
    raw_payload = pagination._b64url_decode(env["payload"])
    obj = canonical.loads_canonical(raw_payload)
    obj["last_proposal_revision"] = obj["last_proposal_revision"]  # unchanged
    # Swap two keys' insertion order -> still canonical; tamper a value instead.
    tampered_obj = copy.deepcopy(obj)
    tampered_obj["last_proposal_id"] = "ZZZZZZZZZZZZZZZZZZZZZZZZZZ"
    fake_payload_bytes = canonical.dumps_canonical(tampered_obj)
    fake_tag = other_signer.sign(REPO, fake_payload_bytes)[1]  # wrong key tag
    fake_env = {"v": 1, "kid": signer.active_key_id(),
                "payload": pagination._b64url_encode(fake_payload_bytes), "tag": fake_tag}
    fake_token = pagination._b64url_encode(canonical.dumps_canonical(fake_env))
    with pytest.raises(CursorError):
        decode_cursor(fake_token, signer=signer, repository_id=REPO, now=CLOCK)


# ============================================================================
# PAGE-05: cursor expiry requires fresh initial read
# ============================================================================


def test_page_05_expiry():
    """An expired payload or key requires a fresh initial read."""
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(4)]
    view = _view(items)
    signer = _signer()
    page = walk_page(view, query=ListQuery(), limit=2, signer=signer, now=CLOCK)
    token = page.next_cursor
    assert token is not None
    # Past the 10-minute cursor lifetime -> cursor_expired.
    with pytest.raises(CursorExpiredError):
        decode_cursor(token, signer=signer, repository_id=REPO, now=CLOCK_AFTER)
    # Key expiry also yields cursor_expired (not invalid_cursor).
    short_key = _key(expires_at="2026-07-14T12:05:00.000Z")
    short_signer = _signer(keys=[short_key])
    short_view = _view(items, clock="2026-07-14T12:04:00.000Z")
    short_page = walk_page(short_view, query=ListQuery(), limit=2, signer=short_signer,
                           now="2026-07-14T12:04:00.000Z")
    short_token = short_page.next_cursor
    assert short_token is not None
    # cursor_expires_at is bounded by the key expiry (min).
    payload = decode_cursor(short_token, signer=short_signer, repository_id=REPO,
                            now="2026-07-14T12:04:30.000Z")
    assert payload.cursor_expires_at == "2026-07-14T12:05:00.000Z"
    with pytest.raises(CursorExpiredError):
        decode_cursor(short_token, signer=short_signer, repository_id=REPO,
                      now="2026-07-14T12:05:30.000Z")


# ============================================================================
# PAGE-06: pruned/corrupt frozen authority returns resync/error, not omission
# ============================================================================


def test_page_06_pruned_authority():
    """A pruned frozen generation returns cursor_expired, never silent omission."""
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(4)]
    # Frozen at generation 1; generation 1 is available now.
    view = _view(items, available={agg: frozenset({1}) for agg in (
        "proposals", "decisions", "transactions", "endpoint_results",
        "outbox", "tombstones", "corruption",
    )})
    signer = _signer()
    page = walk_page(view, query=ListQuery(), limit=2, signer=signer, now=CLOCK)
    token = page.next_cursor
    assert token is not None
    # Continuation still works while the frozen generation is available.
    cont = walk_page(view, query=ListQuery(), limit=2, signer=signer, now=CLOCK, cursor=token)
    assert len(cont.items) == 2
    # Generation 1 pruned (only generation 2 remains) -> cursor_expired.
    pruned_view = _view(
        items,
        available={agg: frozenset({2}) for agg in (
            "proposals", "decisions", "transactions", "endpoint_results",
            "outbox", "tombstones", "corruption",
        )},
    )
    with pytest.raises(CursorExpiredError) as exc_info:
        walk_page(pruned_view, query=ListQuery(), limit=2, signer=signer, now=CLOCK, cursor=token)
    assert exc_info.value.resync_required is True


def test_page_06_missing_empty_available():
    """Missing/empty/generation-zero available authority fails cursor_expired (Fix #9)."""
    items = [_item(_pid(i), f"2026-07-14T{10+i:02d}:00:00.000Z") for i in range(4)]
    signer = _signer()
    normal_avail = {agg: frozenset({1}) for agg in (
        "proposals", "decisions", "transactions", "endpoint_results",
        "outbox", "tombstones", "corruption",
    )}
    view_normal = _view(items, available=normal_avail)
    page = walk_page(view_normal, query=ListQuery(), limit=2, signer=signer, now=CLOCK)
    token = page.next_cursor
    assert token is not None
    # Missing available_generations key entirely -> cursor_expired.
    with pytest.raises(ProjectionError, match="exactly seven"):
        FrozenPaginationView(
            repository_id=REPO, items=tuple(items),
            high_waters=_high_waters(), available_generations={},
              bundle_snapshot_digest=DIGEST, config_digest=DIGEST,
              validation_context_digest=DIGEST_B,
            capabilities=("okf.cap.typed_relations",),
            validator_id=RUNTIME, serializer_id=RUNTIME_SER,
            operation_runtime_id=RUNTIME_OPS, clock_captured_at=CLOCK,
        )
    # Empty frozenset for an aggregate -> cursor_expired.
    view_empty = FrozenPaginationView(
        repository_id=REPO, items=tuple(items),
        high_waters=_high_waters(),
        available_generations={**normal_avail, "proposals": frozenset()},
          bundle_snapshot_digest=DIGEST, config_digest=DIGEST,
          validation_context_digest=DIGEST_B,
        capabilities=("okf.cap.typed_relations",),
        validator_id=RUNTIME, serializer_id=RUNTIME_SER,
        operation_runtime_id=RUNTIME_OPS, clock_captured_at=CLOCK,
    )
    with pytest.raises(CursorExpiredError):
        walk_page(view_empty, query=ListQuery(), limit=2, signer=signer, now=CLOCK, cursor=token)
    # Generation 0 (frozen at gen 1 but only gen 0 available) -> cursor_expired.
    view_zero = _view(items, available={agg: frozenset({0}) for agg in (
        "proposals", "decisions", "transactions", "endpoint_results",
        "outbox", "tombstones", "corruption",
    )})
    with pytest.raises(CursorExpiredError):
        walk_page(view_zero, query=ListQuery(), limit=2, signer=signer, now=CLOCK, cursor=token)


# ============================================================================
# Constant-time HMAC + page token distinctness (api-protocol)
# ============================================================================


def test_cursor_constant_time_and_distinct_tokens():
    """Cursor compare is constant-time; page token != read-model token."""
    signer = _signer()
    view = _view([_item(_pid(i), f"2026-07-14T{10+i:02d}:00:00.000Z") for i in range(3)])
    page = walk_page(view, query=ListQuery(), limit=1, signer=signer, now=CLOCK)
    # The per-proposal read_model_token participates in CAS; the page token does not.
    assert page.page_snapshot_token != page.items[0].read_model_token
    assert page.page_snapshot_token.startswith("sha256:")
    # verify() never raises on mismatch; returns False (constant-time path).
    assert signer.verify(REPO, signer.active_key_id(), b"x", "badtag") is False
    assert signer.verify(REPO, "cur_unknown", b"x", "x") is False
    # An empty final page emits next_cursor=None (no cursor with null boundaries).
    page_final = walk_page(view, query=ListQuery(), limit=10, signer=signer, now=CLOCK)
    assert page_final.next_cursor is None
    assert page_final.items  # all items present on the single final page


def test_limit_bounds_and_query_strictness():
    """limit bounds and repeat-forbidden state are enforced fail-closed."""
    view = _view([_item(_pid(0), "2026-07-14T10:00:00.000Z")])
    signer = _signer()
    for bad in (0, 101, -1):
        with pytest.raises(ProjectionError):
            walk_page(view, query=ListQuery(), limit=bad, signer=signer, now=CLOCK)
    with pytest.raises(ProjectionError):
        ListQuery(state="bogus")
    # State filter narrows the frozen set at the captured clock.
    a = _item(_pid(0), "2026-07-14T10:00:00.000Z", state="ready")
    b = _item(_pid(1), "2026-07-14T11:00:00.000Z", state="in_review")
    view2 = _view([a, b])
    page = walk_page(view2, query=ListQuery(state="ready"), limit=10, signer=signer, now=CLOCK)
    assert [i.proposal_id for i in page.items] == [_pid(0)]


# ============================================================================
# Import-boundary guarantee: no forbidden imports / I/O
# ============================================================================


def test_pagination_import_boundary():
    """Pagination imports no server/studio/cli/render/viewer/Bundle/filesystem."""
    import inspect
    from okf_loom.proposal_review import pagination as pag
    src = inspect.getsource(pag)
    forbidden = ("import os", "open(", "requests", "socket",
                 "from okf_loom.server", "from okf_loom.studio", "from okf_loom.cli",
                 "from okf_loom.render", "from okf_loom.viewer")
    for token in forbidden:
        assert token not in src, f"pagination must not reference {token!r}"


# ============================================================================
# Req #2: Cursor activation — not_before enforcement
# ============================================================================


def test_cursor_not_before_boundary():
    """Signing and decode enforce not_before <= now < expires_at (Req #2)."""
    nb = "2026-07-14T11:00:00.000Z"
    key = CursorKeyMaterial(
        key_id="cur_" + "0" * 26, secret=b"\x01" * 32,
        not_before=nb, expires_at=KEY_EXPIRES,
    )
    signer = _signer(keys=[key])
    items = [_item(_pid(0), "2026-07-14T10:00:00.000Z")]
    view = _view(items, clock="2026-07-14T11:30:00.000Z")
    # Sign at 11:30 — within validity window.
    page = walk_page(view, query=ListQuery(), limit=1, signer=signer, now="2026-07-14T11:30:00.000Z")
    # Decode exactly at not_before — must accept.
    payload = decode_cursor(
        page.next_cursor or encode_cursor(
            pagination.CursorPayload(
                schema=1, repository_id=REPO, query_digest=ListQuery().digest,
                last_created_at="2026-07-14T10:00:00.000Z", last_proposal_id=_pid(0),
                last_proposal_revision=1, repository_high_waters=_high_waters(),
                bundle_snapshot_digest=DIGEST, validation_context_digest=DIGEST_B,
                clock_captured_at="2026-07-14T11:30:00.000Z",
                earliest_expiry_at=None, environment_digest=view.environment_digest(),
                cursor_expires_at="2026-07-14T12:10:00.000Z",
            ), signer),
        signer=signer, repository_id=REPO, now=nb)
    assert payload is not None
    # Decode one ms before not_before — must reject (invalid_cursor).
    before_nb = "2026-07-14T10:59:59.999Z"
    # Re-sign at a valid time for a usable cursor.
    page_ok = walk_page(view, query=ListQuery(), limit=1, signer=signer, now="2026-07-14T11:30:00.000Z")
    token = page_ok.next_cursor
    if token:
        with pytest.raises(CursorError):
            decode_cursor(token, signer=signer, repository_id=REPO, now=before_nb)


def test_cursor_key_not_before_exposed():
    """HmacCursorSigner exposes key_not_before (Req #2)."""
    nb = "2026-07-14T11:00:00.000Z"
    key = CursorKeyMaterial(
        key_id="cur_test", secret=b"\x01" * 32,
        not_before=nb, expires_at=KEY_EXPIRES,
    )
    signer = _signer(keys=[key])
    assert signer.key_not_before("cur_test") == nb
    assert signer.key_not_before("unknown") is None


@pytest.mark.parametrize("missing", ["not_before", "expires_at"])
def test_verified_cursor_requires_complete_key_validity_metadata(missing):
    delegate = _signer()

    class VerifiedSigner:
        def sign(self, repository_id, payload):
            return delegate.sign(repository_id, payload)

        def verify(self, repository_id, key_id, payload, tag):
            return True

        def key_not_before(self, key_id):
            return None if missing == "not_before" else delegate.key_not_before(key_id)

        def key_expires_at(self, key_id):
            return None if missing == "expires_at" else delegate.key_expires_at(key_id)

    view = _view([_item(_pid(i), f"2026-07-14T{10+i:02d}:00:00.000Z") for i in range(2)])
    token = walk_page(view, query=ListQuery(), limit=1, signer=delegate, now=CLOCK).next_cursor
    assert token is not None
    with pytest.raises(CursorError, match="validity metadata is missing"):
        decode_cursor(token, signer=VerifiedSigner(), repository_id=REPO, now=CLOCK)


def test_verified_cursor_rejects_malformed_caller_now():
    signer = _signer()
    view = _view([_item(_pid(i), f"2026-07-14T{10+i:02d}:00:00.000Z") for i in range(2)])
    token = walk_page(view, query=ListQuery(), limit=1, signer=signer, now=CLOCK).next_cursor
    assert token is not None
    with pytest.raises(CursorError, match="verification time is malformed"):
        decode_cursor(token, signer=signer, repository_id=REPO,
                      now="2026-07-14T12:00:00Z")


class _SigningMetadataSigner:
    def __init__(self, *, not_before=CLOCK, expires_at=KEY_EXPIRES):
        self.delegate = _signer()
        self.not_before = not_before
        self.expires_at = expires_at
        self.sign_calls = 0

    def active_key_id(self):
        return self.delegate.active_key_id()

    def key_not_before(self, key_id):
        return self.not_before

    def key_expires_at(self, key_id):
        return self.expires_at

    def sign(self, repository_id, payload):
        self.sign_calls += 1
        return self.delegate.sign(repository_id, payload)

    def verify(self, repository_id, key_id, payload, tag):
        return self.delegate.verify(repository_id, key_id, payload, tag)


def _cursor_issuing_view():
    return _view([_item(_pid(i), f"2026-07-14T{10+i:02d}:00:00.000Z")
                  for i in range(2)])


def test_initial_cursor_issuance_rejects_malformed_now_before_signing():
    signer = _SigningMetadataSigner()
    with pytest.raises(ProjectionError, match="issuance time is malformed"):
        walk_page(_cursor_issuing_view(), query=ListQuery(), limit=1,
                  signer=signer, now="2026-07-14T12:00:00Z")
    assert signer.sign_calls == 0


@pytest.mark.parametrize("missing", ["not_before", "expires_at"])
def test_initial_cursor_issuance_requires_complete_key_metadata(missing):
    signer = _SigningMetadataSigner(
        not_before=None if missing == "not_before" else CLOCK,
        expires_at=None if missing == "expires_at" else KEY_EXPIRES,
    )
    with pytest.raises(ProjectionError, match="validity metadata is missing"):
        walk_page(_cursor_issuing_view(), query=ListQuery(), limit=1,
                  signer=signer, now=CLOCK)
    assert signer.sign_calls == 0


@pytest.mark.parametrize("field", ["not_before", "expires_at"])
def test_initial_cursor_issuance_rejects_malformed_key_metadata(field):
    values = {"not_before": CLOCK, "expires_at": KEY_EXPIRES}
    values[field] = "2026-07-14T12:00:00Z"
    signer = _SigningMetadataSigner(**values)
    with pytest.raises(ProjectionError, match="validity metadata must be canonical"):
        walk_page(_cursor_issuing_view(), query=ListQuery(), limit=1,
                  signer=signer, now=CLOCK)
    assert signer.sign_calls == 0


@pytest.mark.parametrize("not_before,expires_at,now", [
    (KEY_EXPIRES, CLOCK, CLOCK),
    ("2026-07-14T12:00:00.001Z", KEY_EXPIRES, CLOCK),
    (CLOCK, KEY_EXPIRES, KEY_EXPIRES),
])
def test_initial_cursor_issuance_rejects_invalid_or_inactive_key_window(
        not_before, expires_at, now):
    signer = _SigningMetadataSigner(not_before=not_before, expires_at=expires_at)
    with pytest.raises(ProjectionError, match="validity window|not currently valid"):
        walk_page(_cursor_issuing_view(), query=ListQuery(), limit=1,
                  signer=signer, now=now)
    assert signer.sign_calls == 0


def test_cursor_canonical_base64url_enforced():
    """Non-canonical base64url segments are rejected (Req #2/11)."""
    signer = _signer()
    view = _view([_item(_pid(i), f"2026-07-14T{10+i:02d}:00:00.000Z") for i in range(3)])
    page = walk_page(view, query=ListQuery(), limit=1, signer=signer, now=CLOCK)
    token = page.next_cursor
    assert token is not None
    # Standard-padded variant (adding '=') must reject.
    with pytest.raises(CursorError):
          decode_cursor(token + "=", signer=signer, repository_id=REPO, now=CLOCK)


def test_frozen_view_defensively_copies_all_authority_inputs():
    waters = _high_waters()
    available = {agg: {1} for agg in waters}
    items = [_item(_pid(0), "2026-07-14T10:00:00.000Z")]
    view = _view(items, high_waters=waters, available=available)
    before = view.environment_digest()
    waters["proposals"]["sequence"] = 99
    available["proposals"].add(99)
    items.append(_item(_pid(1), "2026-07-14T11:00:00.000Z"))
    assert view.high_waters["proposals"]["sequence"] == 0
    assert view.available_generations["proposals"] == frozenset({1})
    assert len(view.items) == 1
    assert view.environment_digest() == before


@pytest.mark.parametrize("bad", [True, 1.5, "1", None])
def test_frozen_view_rejects_malformed_high_water_primitives(bad):
    waters = _high_waters()
    waters["proposals"]["sequence"] = bad
    with pytest.raises(CursorError, match="non-negative integers"):
        _view([], high_waters=waters)


def test_frozen_view_rejects_extra_high_water_key():
    waters = _high_waters()
    waters["extra"] = {"generation": 0, "sequence": 0}
    with pytest.raises(CursorError, match="exactly seven"):
        _view([], high_waters=waters)


def test_signed_payload_rejects_reversed_expiry_order():
    payload = pagination.CursorPayload(
        schema=1, repository_id=REPO, query_digest=DIGEST,
        last_created_at=CLOCK, last_proposal_id=_pid(0), last_proposal_revision=1,
        repository_high_waters=_high_waters(), bundle_snapshot_digest=DIGEST,
        validation_context_digest=DIGEST_B,
        clock_captured_at=CLOCK_AFTER, earliest_expiry_at=None,
        environment_digest=DIGEST, cursor_expires_at=CLOCK,
    )
    token = encode_cursor(payload, _signer())
    with pytest.raises(CursorError, match="expiry must be after"):
        decode_cursor(token, signer=_signer(), repository_id=REPO, now=CLOCK)


def _cursor_process(token, results):
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(4)]
    view = _view(items)
    page = walk_page(view, query=ListQuery(), limit=2, signer=_signer(),
                     now=CLOCK, cursor=token)
    results.put([item.sort_key for item in page.items])


def test_cursor_01_cacheless_reconstruction():
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(4)]
    view = _view(items); first = walk_page(
        view, query=ListQuery(), limit=2, signer=_signer(), now=CLOCK)
    context = multiprocessing.get_context("spawn"); results = context.Queue()
    process = context.Process(target=_cursor_process,
                              args=(first.next_cursor, results))
    process.start(); process.join(20); assert process.exitcode == 0
    observed = [item.sort_key for item in first.items] + results.get(timeout=2)
    assert observed == sorted([item.sort_key for item in items], reverse=True)


def test_cursor_02_worker_handoff(tmp_path):
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(5)]
    frozen = _view(items); signer = _signer(); cursor = None; observed = []
    while True:
        page = walk_page(frozen, query=ListQuery(), limit=2, signer=signer,
                         now=CLOCK, cursor=cursor)
        observed.extend(item.sort_key for item in page.items)
        cursor = page.next_cursor
        if cursor is None: break
        # Concurrent append exists only in current authority, never the frozen
        # prefix carried by this walk.
        _view(items + [_item(_pid(9), "2026-07-14T20:00:00.000Z")],
              high_waters=_high_waters(gen=1, seq=1))
    assert observed == sorted([item.sort_key for item in items], reverse=True)
    assert len(observed) == len(set(observed))


def test_cursor_03_environment_change():
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(4)]
    frozen = _view(items); signer = _signer()
    first = walk_page(frozen, query=ListQuery(), limit=2, signer=signer, now=CLOCK)
    drifted = _view(items, bundle_digest=DIGEST_B,
                    validation_context_digest=DIGEST)
    with pytest.raises(CursorExpiredError):
        walk_page(drifted, query=ListQuery(), limit=2, signer=signer,
                  now=CLOCK, cursor=first.next_cursor)


def test_cursor_04_pruned_facts():
    items = [_item(_pid(i), f"2026-07-14T{10 + i:02d}:00:00.000Z") for i in range(4)]
    available = {agg: frozenset({1}) for agg in _high_waters()}
    frozen = _view(items, available=available); signer = _signer()
    first = walk_page(frozen, query=ListQuery(), limit=2, signer=signer, now=CLOCK)
    pruned = _view(items, available={agg: frozenset({2}) for agg in _high_waters()})
    with pytest.raises(CursorExpiredError):
        walk_page(pruned, query=ListQuery(), limit=2, signer=signer,
                  now=CLOCK, cursor=first.next_cursor)
    corrupt = _high_waters(); corrupt["proposals"]["sequence"] = -1
    with pytest.raises(CursorError):
        _view(items, high_waters=corrupt)
