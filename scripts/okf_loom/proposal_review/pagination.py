"""Pure snapshot-isolated pagination for Proposal / Diff Review v1 (Foundation B).

Stateless, cryptographically signed list pagination over an immutable
repository snapshot plus explicit bundle/runtime/clock facts (SPEC section 10).
The module is side-effect-free: it never touches the clock (an explicit
``now`` is supplied), the filesystem, the network, or any
server/Studio/CLI/render/viewer/Bundle code.

Guarantees (SPEC section 10 / 12):

* unique descending ``(created_at, proposal_id, proposal_revision)`` order;
* the exclusive last tuple ensures no skip and no duplicate under a fixed
  snapshot (PAGE-01/02);
* every derived input (high waters, bundle snapshot, environment digest,
  captured clock, earliest expiry) is frozen into the signed cursor payload,
  so concurrent appends / bundle / runtime / later-expiry changes never alter
  later pages (PAGE-03);
* cursor tamper, query mismatch, non-canonical re-serialization, unknown key,
  and tag mismatch fail closed as ``invalid_cursor``; expired payload/key or
  pruned frozen authority fail as ``cursor_expired`` with ``resync_required``
  (PAGE-04/05/06);
* the page token (``page_snapshot_token``) is distinct from the per-proposal
  read-model token and MUST NOT authorize proposal mutation;
* cursor comparison uses constant-time HMAC verification.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .canonical import canonical_digest, dumps_canonical, loads_canonical
from .errors import CursorError, CursorExpiredError, ProjectionError
from .ports import CursorSigner
from . import ids

#: Cursor context/string and maximum lifetime (SPEC section 10).
CURSOR_CONTEXT: bytes = b"okf-proposal-cursor-v1\0"
CURSOR_MAX_LIFETIME_SECONDS: int = 10 * 60  # 10 minutes

#: Page ``limit`` default and bounds (SPEC section 4).
PAGE_LIMIT_DEFAULT: int = 50
PAGE_LIMIT_MIN: int = 1
PAGE_LIMIT_MAX: int = 100

#: The closed set of lifecycle states that may be filtered on (SPEC 7.2).
_FILTER_STATES: frozenset[str] = frozenset({
    "in_review", "ready", "conflicted", "applied", "withdrawn",
    "superseded", "expired", "audit_expired",
})


# ---------------------------------------------------------------------------
# Frozen view + list item
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalListItem:
    """One frozen proposal summary used for list ordering/filtering.

    ``state`` is the proposal's derived lifecycle state evaluated at the
    captured clock instant by the caller (using :mod:`projectors`).  The
    pagination layer never re-derives state, so later clock/expiry changes do
    not alter frozen pages.
    """

    created_at: str
    proposal_id: str
    proposal_revision: int
    state: str
    expires_at: str | None
    read_model_token: str
    summary: str = ""
    author: str = ""

    def __post_init__(self) -> None:
        ids.validate_timestamp(self.created_at, name="created_at")
        ids.validate_proposal_id(self.proposal_id)
        ids.validate_revision(self.proposal_revision, name="proposal_revision")
        if self.state not in _FILTER_STATES:
            raise ProjectionError("proposal list item has an invalid state")
        if self.expires_at is not None:
            ids.validate_timestamp(self.expires_at, name="expires_at")
            if self.expires_at < self.created_at:
                raise ProjectionError("proposal list item expires before creation")
        ids.validate_digest(self.read_model_token, name="read_model_token")

    @property
    def sort_key(self) -> tuple[str, str, int]:
        return (self.created_at, self.proposal_id, self.proposal_revision)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "proposal_revision": self.proposal_revision,
            "state": self.state,
            "expires_at": self.expires_at,
            "read_model_token": self.read_model_token,
            "summary": self.summary,
            "author": self.author,
        }


@dataclass(frozen=True)
class FrozenPaginationView:
    """The complete frozen projection snapshot inputs + items for one walk.

    Captured once under the repository append/rotation lock at the initial
    read and reused verbatim for every continuation page.  ``high_waters`` is
    the exact seven-aggregate high-water map; ``available_generations`` is the
    set of generation numbers still reconstructable for each aggregate (used
    to detect pruned frozen authority, PAGE-06).
    """

    repository_id: str
    items: tuple[ProposalListItem, ...]
    high_waters: Mapping[str, Mapping[str, int]]
    available_generations: Mapping[str, frozenset[int]]
    bundle_snapshot_digest: str
    config_digest: str
    validation_context_digest: str
    capabilities: tuple[str, ...]
    validator_id: str
    serializer_id: str
    operation_runtime_id: str
    clock_captured_at: str

    def __post_init__(self) -> None:
        ids.validate_repository_id(self.repository_id)
        _validate_high_waters(self.high_waters)
        if set(self.available_generations) != set(_AGGREGATES):
            raise ProjectionError("available_generations must have exactly seven aggregate keys")
        frozen_waters = {
            agg: MappingProxyType({"generation": self.high_waters[agg]["generation"],
                                   "sequence": self.high_waters[agg]["sequence"]})
            for agg in _AGGREGATES
        }
        if any(not isinstance(item, ProposalListItem) for item in self.items):
            raise ProjectionError("items must contain only ProposalListItem values")
        frozen_available: dict[str, frozenset[int]] = {}
        for agg in _AGGREGATES:
            values = self.available_generations[agg]
            if not isinstance(values, (set, frozenset)):
                raise ProjectionError("available generations must be a set of integers")
            materialized = tuple(values)
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in materialized):
                raise ProjectionError("available generations must be non-negative integers")
            frozen_available[agg] = frozenset(materialized)
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "high_waters", MappingProxyType(frozen_waters))
        object.__setattr__(self, "available_generations", MappingProxyType(frozen_available))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        if (list(self.capabilities) != sorted(set(self.capabilities))
                or any(not isinstance(cap, str)
                       or re.fullmatch(r"okf\.cap\.[a-z0-9_.-]+", cap) is None
                       for cap in self.capabilities)):
            raise ProjectionError("capabilities must be sorted unique canonical ids")
        ids.validate_digest(self.bundle_snapshot_digest, name="bundle_snapshot_digest")
        ids.validate_digest(self.config_digest, name="config_digest")
        ids.validate_digest(self.validation_context_digest, name="validation_context_digest")
        ids.validate_runtime_identity(self.validator_id, name="validator_id")
        ids.validate_runtime_identity(self.serializer_id, name="serializer_id")
        ids.validate_runtime_identity(self.operation_runtime_id, name="operation_runtime_id")
        ids.validate_timestamp(self.clock_captured_at, name="clock_captured_at")

    def environment_digest(self) -> str:
        return canonical_digest({
            "config_digest": self.config_digest,
            "validation_context_digest": self.validation_context_digest,
            "capabilities_digest": canonical_digest(list(self.capabilities)),
            "validator_id": self.validator_id,
            "serializer_id": self.serializer_id,
            "operation_runtime_id": self.operation_runtime_id,
        })

    def earliest_expiry_at(self) -> str | None:
        expiries = [item.expires_at for item in self.items if item.expires_at is not None]
        return min(expiries) if expiries else None


# ---------------------------------------------------------------------------
# Cursor payload + envelope
# ---------------------------------------------------------------------------


_AGGREGATES = ("proposals", "decisions", "transactions", "endpoint_results",
               "outbox", "tombstones", "corruption")


@dataclass(frozen=True)
class CursorPayload:
    """Exact closed signed cursor payload (SPEC section 10 line 849)."""

    schema: int
    repository_id: str
    query_digest: str
    last_created_at: str
    last_proposal_id: str
    last_proposal_revision: int
    repository_high_waters: Mapping[str, Mapping[str, int]]
    bundle_snapshot_digest: str
    validation_context_digest: str
    clock_captured_at: str
    earliest_expiry_at: str | None
    environment_digest: str
    cursor_expires_at: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "repository_id": self.repository_id,
            "query_digest": self.query_digest,
            "last_created_at": self.last_created_at,
            "last_proposal_id": self.last_proposal_id,
            "last_proposal_revision": self.last_proposal_revision,
            "repository_high_waters": {
                agg: {"generation": self.repository_high_waters[agg]["generation"],
                      "sequence": self.repository_high_waters[agg]["sequence"]}
                for agg in _AGGREGATES
            },
            "bundle_snapshot_digest": self.bundle_snapshot_digest,
            "validation_context_digest": self.validation_context_digest,
            "clock_captured_at": self.clock_captured_at,
            "earliest_expiry_at": self.earliest_expiry_at,
            "environment_digest": self.environment_digest,
            "cursor_expires_at": self.cursor_expires_at,
        }

    def canonical_bytes(self) -> bytes:
        return dumps_canonical(self.to_mapping())


def _validate_high_waters(high_waters: Mapping[str, Mapping[str, int]]) -> None:
    if not isinstance(high_waters, Mapping) or set(high_waters) != set(_AGGREGATES):
        raise CursorError("repository_high_waters must have exactly seven aggregate keys")
    for agg in _AGGREGATES:
        if agg not in high_waters:
            raise CursorError(f"repository_high_waters missing aggregate {agg!r}")
        entry = high_waters[agg]
        if not isinstance(entry, Mapping):
            raise CursorError(f"high_water entry for {agg!r} must be an object")
        if set(entry) != {"generation", "sequence"}:
            raise CursorError(f"high_water entry for {agg!r} must have generation+sequence")
        gen = entry["generation"]
        seq = entry["sequence"]
        if (isinstance(gen, bool) or isinstance(seq, bool)
                or not isinstance(gen, int) or not isinstance(seq, int)
                  or gen < 0 or seq < 0 or gen > (1 << 53) - 1 or seq > (1 << 53) - 1):
            raise CursorError(f"high_water values for {agg!r} must be non-negative integers")


def cursor_expires_at(
    *,
    clock_captured_at: str,
    active_key_expires_at: str,
    earliest_expiry_at: str | None,
) -> str:
    """Exact cursor expiry = ``min(clock+10m, key expires_at, earliest_expiry)``.

    SPEC section 10 line 853.  The 10-minute lifetime is computed lexically on
    the millisecond-precision UTC timestamps, which is exact for RFC 3339 UTC.
    """
    clock_plus = _plus_seconds(clock_captured_at, CURSOR_MAX_LIFETIME_SECONDS)
    candidates = [clock_plus, active_key_expires_at]
    if earliest_expiry_at is not None:
        candidates.append(earliest_expiry_at)
    return min(candidates)


def _plus_seconds(timestamp: str, seconds: int) -> str:
    import datetime as _dt
    base = _dt.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    nxt = base + _dt.timedelta(seconds=seconds)
    return nxt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{nxt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# HMAC-SHA-256 cursor signer (pure, explicit key material)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CursorKeyMaterial:
    """One explicit cursor signing key (32-byte secret + validity window)."""

    key_id: str
    secret: bytes
    not_before: str
    expires_at: str

    def __post_init__(self) -> None:
        if len(self.secret) != 32:
            raise ProjectionError("cursor key secret must be exactly 32 bytes")
        try:
            ids.validate_timestamp(self.not_before, name="not_before")
            ids.validate_timestamp(self.expires_at, name="expires_at")
        except Exception as exc:
            raise ProjectionError("cursor key validity metadata must be canonical timestamps") from exc
        if self.not_before >= self.expires_at:
            raise ProjectionError("cursor key not_before must be < expires_at")


@dataclass
class HmacCursorSigner(CursorSigner):
    """Pure HMAC-SHA-256 cursor signer over explicit key material.

    Implements the exact SPEC section 10 algorithm:
    ``T = HMAC-SHA-256(key, "okf-proposal-cursor-v1\\0" || repository_id || "\\0" || P)``.
    Verification is constant-time.  No key store, disk, or network is used.
    """

    keys: dict[str, CursorKeyMaterial] = field(default_factory=dict)
    _active_key_id: str = ""

    def __post_init__(self) -> None:
        if not self.keys:
            raise ProjectionError("HmacCursorSigner requires at least one key")
        if self._active_key_id not in self.keys:
            # default to the key with the latest not_before that is still valid
            self._active_key_id = next(iter(self.keys))

    @classmethod
    def from_keys(cls, keys: Sequence[CursorKeyMaterial], active_key_id: str | None = None) -> "HmacCursorSigner":
        index = {k.key_id: k for k in keys}
        if len(index) != len(keys):
            raise ProjectionError("cursor key ids must be unique")
        active = active_key_id if active_key_id is not None else keys[0].key_id
        return cls(keys=index, _active_key_id=active)

    def active_key_id(self) -> str:
        return self._active_key_id

    def key_expires_at(self, key_id: str) -> str | None:
        material = self.keys.get(key_id)
        return material.expires_at if material is not None else None

    def key_not_before(self, key_id: str) -> str | None:
        material = self.keys.get(key_id)
        return material.not_before if material is not None else None

    def _tag(self, repository_id: str, payload: bytes, secret: bytes) -> bytes:
        msg = CURSOR_CONTEXT + repository_id.encode("utf-8") + b"\0" + payload
        return hmac.new(secret, msg, hashlib.sha256).digest()

    def sign(self, repository_id: str, payload: bytes) -> tuple[str, str]:
        material = self.keys[self._active_key_id]
        tag = self._tag(repository_id, payload, material.secret)
        return material.key_id, _b64url_encode(tag)

    def verify(self, repository_id: str, key_id: str, payload: bytes, tag_base64url: str) -> bool:
        material = self.keys.get(key_id)
        if material is None:
            return False
        try:
            provided = _b64url_decode(tag_base64url)
        except CursorError:
            return False
        expected = self._tag(repository_id, payload, material.secret)
        return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# base64url (unpadded, canonical) helpers
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    if "=" in text:
        raise CursorError("cursor base64url segment must not contain padding")
    # Enforce canonical base64url alphabet: only [A-Za-z0-9_-] allowed (Fix #11).
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise CursorError("cursor base64url segment contains non-canonical characters")
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except Exception as exc:
        raise CursorError(f"cursor base64url segment is not valid: {exc}") from exc


def _b64url_canonical(text: str) -> bool:
    """True iff ``text`` is canonical unpadded base64url (re-encoding is stable)."""
    if "=" in text:
        return False
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_-]*", text):
        return False
    try:
        decoded = _b64url_decode(text)
    except CursorError:
        return False
    return _b64url_encode(decoded) == text


# ---------------------------------------------------------------------------
# Cursor encode / decode
# ---------------------------------------------------------------------------


def encode_cursor(payload: CursorPayload, signer: CursorSigner) -> str:
    """Encode a signed cursor token (SPEC section 10 line 855)."""
    payload_bytes = payload.canonical_bytes()
    kid, tag = signer.sign(payload.repository_id, payload_bytes)
    envelope = {
        "v": 1,
        "kid": kid,
        "payload": _b64url_encode(payload_bytes),
        "tag": tag,
    }
    envelope_bytes = dumps_canonical(envelope)
    return _b64url_encode(envelope_bytes)


def decode_cursor(
    token: str,
    *,
    signer: CursorSigner,
    repository_id: str,
    now: str,
    expected_query_digest: str | None = None,
) -> CursorPayload:
    """Decode + verify a signed cursor token (SPEC section 10 line 856).

    Distinguishes ``invalid_cursor`` (malformed / tampered / unknown key /
    non-canonical / repository or query mismatch) from ``cursor_expired``
    (expired payload or key).  Tag verification is constant-time.
    """
    # Outer envelope must be canonical base64url without padding (Fix #11).
    if not _b64url_canonical(token):
        raise CursorError("cursor outer envelope is not canonical base64url")
    try:
        envelope_bytes = _b64url_decode(token)
    except CursorError:
        raise
    try:
        envelope = loads_canonical(envelope_bytes)
    except Exception as exc:
        raise CursorError(f"cursor envelope is not canonical JSON: {exc}") from exc
    if set(envelope) != {"v", "kid", "payload", "tag"}:
        raise CursorError("cursor envelope has unknown or missing fields")
    if envelope["v"] != 1:
        raise CursorError("unsupported cursor envelope version")
    kid = envelope["kid"]
    payload_b64 = envelope["payload"]
    tag = envelope["tag"]
    if not isinstance(kid, str) or not isinstance(payload_b64, str) or not isinstance(tag, str):
        raise CursorError("cursor envelope members must be strings")
    # Payload and tag must be canonical base64url (Fix #11).
    if not _b64url_canonical(payload_b64):
        raise CursorError("cursor payload segment is not canonical base64url")
    if not _b64url_canonical(tag):
        raise CursorError("cursor tag segment is not canonical base64url")

    # Payload must re-serialize canonically (reject non-canonical input).
    try:
        payload_bytes = _b64url_decode(payload_b64)
    except CursorError:
        raise
    try:
        payload_obj = loads_canonical(payload_bytes)
    except Exception as exc:
        raise CursorError(f"cursor payload is not canonical JSON: {exc}") from exc
    if dumps_canonical(payload_obj) != payload_bytes:
        raise CursorError("cursor payload is not canonical")

    # Signature: constant-time verify; unknown key -> invalid_cursor.
    if not signer.verify(repository_id, kid, payload_bytes, tag):
        raise CursorError("cursor signature verification failed")

    try:
        now = ids.validate_timestamp(now, name="now")
    except Exception as exc:
        raise CursorError("cursor verification time is malformed") from exc

    payload = _payload_from_mapping(payload_obj)

    # Repository mismatch -> invalid_cursor.
    if payload.repository_id != repository_id:
        raise CursorError("cursor repository_id mismatch")

    # Query mismatch -> invalid_cursor.
    if expected_query_digest is not None and payload.query_digest != expected_query_digest:
        raise CursorError("cursor query mismatch")

    # Key validity window: not_before <= now < expires_at (Fix #2 / cursor activation).
    try:
        key_not_before = signer.key_not_before(kid)
        key_expires = signer.key_expires_at(kid)
        if key_not_before is None or key_expires is None:
            raise CursorError("cursor signing key validity metadata is missing")
        key_not_before = ids.validate_timestamp(key_not_before, name="key_not_before")
        key_expires = ids.validate_timestamp(key_expires, name="key_expires_at")
    except CursorError:
        raise
    except Exception as exc:
        raise CursorError("cursor signing key validity metadata is malformed") from exc
    if now < key_not_before:
        raise CursorError("cursor signing key is not yet valid (before not_before)")
    if now >= key_expires:
        raise CursorExpiredError("cursor signing key has expired")

    # Payload expiry -> cursor_expired.
    if now >= payload.cursor_expires_at:
        raise CursorExpiredError("cursor payload has expired")

    return payload


def _payload_from_mapping(obj: Mapping[str, Any]) -> CursorPayload:
    if not isinstance(obj, Mapping):
        raise CursorError("cursor payload must be an object")
    allowed = {
        "schema", "repository_id", "query_digest", "last_created_at",
        "last_proposal_id", "last_proposal_revision", "repository_high_waters",
        "bundle_snapshot_digest", "validation_context_digest", "clock_captured_at", "earliest_expiry_at",
        "environment_digest", "cursor_expires_at",
    }
    if set(obj) != allowed:
        raise CursorError("cursor payload has unknown or missing fields")
    schema = obj["schema"]
    if schema != 1:
        raise CursorError("unsupported cursor payload schema")
    high_waters = obj["repository_high_waters"]
    if not isinstance(high_waters, Mapping):
        raise CursorError("repository_high_waters must be an object")
    _validate_high_waters(high_waters)
    rev = obj["last_proposal_revision"]
    if (isinstance(rev, bool) or not isinstance(rev, int) or rev < 1):
        raise CursorError("last_proposal_revision must be a positive integer")
    try:
        ids.validate_repository_id(obj["repository_id"])
        for name in ("query_digest", "bundle_snapshot_digest", "validation_context_digest", "environment_digest"):
            ids.validate_digest(obj[name], name=name)
        ids.validate_proposal_id(obj["last_proposal_id"])
        for name in ("last_created_at", "clock_captured_at", "cursor_expires_at"):
            ids.validate_timestamp(obj[name], name=name)
        if obj["earliest_expiry_at"] is not None:
            ids.validate_timestamp(obj["earliest_expiry_at"], name="earliest_expiry_at")
    except Exception as exc:
        raise CursorError("cursor payload contains a malformed authority primitive") from exc
    if obj["cursor_expires_at"] <= obj["clock_captured_at"]:
        raise CursorError("cursor expiry must be after captured clock")
    if obj["earliest_expiry_at"] is not None and obj["earliest_expiry_at"] < obj["clock_captured_at"]:
        raise CursorError("earliest expiry precedes captured clock")
    return CursorPayload(
        schema=schema,
        repository_id=obj["repository_id"],
        query_digest=obj["query_digest"],
        last_created_at=obj["last_created_at"],
        last_proposal_id=obj["last_proposal_id"],
        last_proposal_revision=rev,
        repository_high_waters=MappingProxyType({
            agg: {"generation": int(high_waters[agg]["generation"]),
                  "sequence": int(high_waters[agg]["sequence"])}
          for agg in _AGGREGATES
          }),
        bundle_snapshot_digest=obj["bundle_snapshot_digest"],
        validation_context_digest=obj["validation_context_digest"],
        clock_captured_at=obj["clock_captured_at"],
        earliest_expiry_at=obj["earliest_expiry_at"],
        environment_digest=obj["environment_digest"],
        cursor_expires_at=obj["cursor_expires_at"],
    )


# ---------------------------------------------------------------------------
# Query normalization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListQuery:
    """Strict list query (SPEC section 10): optional, repeat-forbidden state."""

    state: str | None = None

    def __post_init__(self) -> None:
        if self.state is not None and self.state not in _FILTER_STATES:
            raise ProjectionError(f"state filter must be one of {sorted(_FILTER_STATES)}")

    @property
    def digest(self) -> str:
        return canonical_digest({"state": self.state})


# ---------------------------------------------------------------------------
# Page snapshot token + page result
# ---------------------------------------------------------------------------


def page_snapshot_token(
    *,
    repository_id: str,
    high_waters: Mapping[str, Mapping[str, int]],
    bundle_snapshot_digest: str,
    validation_context_digest: str,
    environment_digest: str,
    clock_captured_at: str,
    earliest_expiry_at: str | None,
    query_digest: str,
    first_sort_key: tuple[str, str, int] | None,
    last_sort_key: tuple[str, str, int],
) -> str:
    """Display/pagination-consistency token over frozen inputs + boundaries.

    This is consistency evidence only and MUST NOT authorize proposal mutation
    (SPEC section 10 line 861).  It is intentionally distinct from any
    per-proposal read-model token.
    """
    body = {
        "repository_id": repository_id,
        "high_waters": {agg: dict(high_waters[agg]) for agg in _AGGREGATES},
        "bundle_snapshot_digest": bundle_snapshot_digest,
        "validation_context_digest": validation_context_digest,
        "environment_digest": environment_digest,
        "clock_captured_at": clock_captured_at,
        "earliest_expiry_at": earliest_expiry_at,
        "query_digest": query_digest,
        "first_sort_key": list(first_sort_key) if first_sort_key is not None else None,
        "last_sort_key": list(last_sort_key) if last_sort_key is not None else None,
    }
    return canonical_digest(body)


@dataclass(frozen=True)
class Page:
    """One closed list page (SPEC section 10 line 860)."""

    schema: int
    items: tuple[ProposalListItem, ...]
    next_cursor: str | None
    page_snapshot_token: str
    snapshot_expires_at: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "items": [item.to_mapping() for item in self.items],
            "next_cursor": self.next_cursor,
            "page_snapshot_token": self.page_snapshot_token,
            "snapshot_expires_at": self.snapshot_expires_at,
        }


# ---------------------------------------------------------------------------
# Page walk
# ---------------------------------------------------------------------------


def _ordered_items(items: Iterable[ProposalListItem]) -> list[ProposalListItem]:
    return sorted(items, key=lambda i: i.sort_key, reverse=True)


def _after(last: tuple[str, str, int], items: Iterable[ProposalListItem]) -> list[ProposalListItem]:
    """Items strictly before ``last`` in descending order (exclusive boundary)."""
    return [i for i in items if i.sort_key < last]


def _active_signing_window(signer: CursorSigner, now: str) -> tuple[str, str]:
    """Validate all caller/key time authority before a cursor can be signed."""
    try:
        now = ids.validate_timestamp(now, name="now")
    except Exception as exc:
        raise ProjectionError("cursor issuance time is malformed") from exc

    key_id = signer.active_key_id()
    key_not_before = signer.key_not_before(key_id)
    key_expires = signer.key_expires_at(key_id)
    if key_not_before is None or key_expires is None:
        raise ProjectionError("active cursor key validity metadata is missing")
    try:
        key_not_before = ids.validate_timestamp(key_not_before, name="key_not_before")
        key_expires = ids.validate_timestamp(key_expires, name="key_expires_at")
    except Exception as exc:
        raise ProjectionError(
            "active cursor key validity metadata must be canonical timestamps"
        ) from exc
    if key_not_before >= key_expires:
        raise ProjectionError("active cursor key validity window is invalid")
    if not (key_not_before <= now < key_expires):
        raise ProjectionError("active cursor key is not currently valid")
    return key_not_before, key_expires


def walk_page(
    view: FrozenPaginationView,
    *,
    query: ListQuery,
    limit: int,
    signer: CursorSigner,
    now: str,
    cursor: str | None = None,
) -> Page:
    """Walk one snapshot-isolated page over the frozen view (PAGE-01..06).

    Initial read (``cursor is None``) freezes every derived input.  Continuation
    pages decode + verify the signed cursor, re-check the frozen authority
    matches the supplied view (PAGE-03) and that the frozen generations are
    still reconstructable (PAGE-06), then advance by the exclusive last tuple.
    Returns a closed :class:`Page`; an empty/final page emits
    ``next_cursor=None``.
    """
    if limit < PAGE_LIMIT_MIN or limit > PAGE_LIMIT_MAX:
        raise ProjectionError(
            f"limit must be in {PAGE_LIMIT_MIN}..{PAGE_LIMIT_MAX}"
        )
    _validate_high_waters(view.high_waters)
    _, key_expires = _active_signing_window(signer, now)

    earliest = view.earliest_expiry_at()
    expires_at = cursor_expires_at(
        clock_captured_at=view.clock_captured_at,
        active_key_expires_at=key_expires,
        earliest_expiry_at=earliest,
    )

    # Apply the (frozen) state filter and stable descending order.
    ordered = _ordered_items(
        i for i in view.items if query.state is None or i.state == query.state
    )

    if cursor is None:
        page_items = ordered[:limit]
        last_key = page_items[-1].sort_key if page_items else None
        first_key = page_items[0].sort_key if page_items else None
        has_more = len(ordered) > limit
    else:
        payload = decode_cursor(
            cursor, signer=signer, repository_id=view.repository_id,
            now=now, expected_query_digest=query.digest,
        )
        # Re-verify frozen authority matches the supplied view (PAGE-03/06).
        _verify_frozen_authority(payload, view)
        last = (payload.last_created_at, payload.last_proposal_id,
                payload.last_proposal_revision)
        remaining = _after(last, ordered)
        page_items = remaining[:limit]
        last_key = page_items[-1].sort_key if page_items else None
        first_key = page_items[0].sort_key if page_items else None
        has_more = len(remaining) > limit

    # Build next cursor only when more frozen items remain.
    next_cursor: str | None = None
    if has_more and last_key is not None:
        next_payload = CursorPayload(
            schema=1,
            repository_id=view.repository_id,
            query_digest=query.digest,
            last_created_at=last_key[0],
            last_proposal_id=last_key[1],
            last_proposal_revision=last_key[2],
            repository_high_waters=view.high_waters,
            bundle_snapshot_digest=view.bundle_snapshot_digest,
            validation_context_digest=view.validation_context_digest,
            clock_captured_at=view.clock_captured_at,
            earliest_expiry_at=earliest,
            environment_digest=view.environment_digest(),
            cursor_expires_at=expires_at,
        )
        next_cursor = encode_cursor(next_payload, signer)

    snap_token = page_snapshot_token(
        repository_id=view.repository_id,
        high_waters=view.high_waters,
        bundle_snapshot_digest=view.bundle_snapshot_digest,
        validation_context_digest=view.validation_context_digest,
        environment_digest=view.environment_digest(),
        clock_captured_at=view.clock_captured_at,
        earliest_expiry_at=earliest,
        query_digest=query.digest,
        first_sort_key=first_key,
        last_sort_key=last_key,  # type: ignore[arg-type]
    )
    return Page(
        schema=1,
        items=tuple(page_items),
        next_cursor=next_cursor,
        page_snapshot_token=snap_token,
        snapshot_expires_at=expires_at,
    )


def _verify_frozen_authority(payload: CursorPayload, view: FrozenPaginationView) -> None:
    """Re-check that the cursor's frozen inputs match the supplied view.

    Mismatch means the frozen projection inputs changed (concurrent append
    advanced the pinned snapshot, the bundle/runtime environment drifted, or
    the frozen generation was pruned and the view fell back to a different
    snapshot).  All such cases require a fresh initial read (PAGE-03/06).
    """
    if payload.repository_id != view.repository_id:
        raise CursorExpiredError("cursor repository no longer matches the view")
    for agg in _AGGREGATES:
        cur = payload.repository_high_waters[agg]
        vw = view.high_waters[agg]
        if cur["generation"] != vw["generation"] or cur["sequence"] != vw["sequence"]:
            raise CursorExpiredError(
                f"frozen high-water for {agg!r} no longer matches the view"
            )
    if payload.bundle_snapshot_digest != view.bundle_snapshot_digest:
        raise CursorExpiredError("frozen bundle snapshot digest changed")
    if payload.validation_context_digest != view.validation_context_digest:
        raise CursorExpiredError("frozen validation context digest changed")
    if payload.environment_digest != view.environment_digest():
        raise CursorExpiredError("frozen environment digest changed")
    if payload.clock_captured_at != view.clock_captured_at:
        raise CursorExpiredError("frozen captured clock changed")
    # PAGE-06: the frozen generation must still be reconstructable.
    # Missing or empty available generation authority for any aggregate MUST
    # fail cursor_expired (Fix #9) — never silently skip.
    for agg in _AGGREGATES:
        gen = view.high_waters[agg]["generation"]
        available = view.available_generations.get(agg)
        if not available:
            raise CursorExpiredError(
                f"available generation authority for {agg!r} is missing or empty")
        if gen not in available:
            raise CursorExpiredError(
                f"frozen generation {gen} for {agg!r} was pruned"
            )


__all__ = [
    "CURSOR_CONTEXT",
    "CURSOR_MAX_LIFETIME_SECONDS",
    "PAGE_LIMIT_DEFAULT",
    "PAGE_LIMIT_MIN",
    "PAGE_LIMIT_MAX",
    "ProposalListItem",
    "FrozenPaginationView",
    "CursorPayload",
    "cursor_expires_at",
    "CursorKeyMaterial",
    "HmacCursorSigner",
    "encode_cursor",
    "decode_cursor",
    "ListQuery",
    "page_snapshot_token",
    "Page",
    "walk_page",
]
