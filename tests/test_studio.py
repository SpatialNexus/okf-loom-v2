"""Tests for the live studio logic layer (``okf_loom.studio``).

Covers the framework-free core: ``rev_of`` / ``new_id``, the :class:`EventBus`
fan-out + drop/resync behaviour, and the :class:`Studio` session feeds
(events / comments / presence / undo). The HTTP-layer tests live in
``test_live.py``; these exercise the reusable logic directly.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

from okf_loom.studio import (
    CommentMutationError,
    EventBus,
    Studio,
    new_id,
    rev_of,
    wait_for_work,
)


# ---------------------------------------------------------------------------
# rev_of + new_id
# ---------------------------------------------------------------------------

def test_rev_of_is_sha1_prefix_and_stable() -> None:
    assert rev_of("hello") == "aaf4c61ddcc5"
    assert rev_of(b"hello") == rev_of("hello")
    assert rev_of("hello") != rev_of("world")


def test_new_ids_are_unique_and_sortable() -> None:
    ids = [new_id() for _ in range(200)]
    assert len(set(ids)) == 200
    # Timestamp-prefixed → lexicographic order tracks creation order (modulo ms).
    later = new_id()
    time.sleep(0.002)
    even_later = new_id()
    assert even_later >= later


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

def test_event_bus_publishes_to_all_subscribers() -> None:
    bus = EventBus(max_queue=16)
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish({"type": "changed", "ids": ["x"]})
    assert a.get(timeout=1) == {"type": "changed", "ids": ["x"]}
    assert b.get(timeout=1) == {"type": "changed", "ids": ["x"]}
    assert bus.client_count() == 2


def test_event_bus_drops_and_resyncs_slow_client() -> None:
    """A full queue (slow client) gets deltas dropped + one resync sentinel."""
    bus = EventBus(max_queue=2)
    q = bus.subscribe()
    for i in range(5):  # overflow the 2-slot queue
        bus.publish({"type": "changed", "n": i})
    drained = []
    while True:
        try:
            drained.append(q.get_nowait())
        except queue.Empty:
            break
    # Exactly one resync survives the overflow; the slow client is told to
    # re-fetch current state rather than seeing a silently-truncated stream.
    assert any(e.get("type") == "resync" for e in drained)


def test_event_bus_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.publish({"type": "changed"})
    with pytest.raises(queue.Empty):
        q.get_nowait()
    assert bus.client_count() == 0


# ---------------------------------------------------------------------------
# Studio — events feed
# ---------------------------------------------------------------------------

@pytest.fixture()
def studio(tmp_path: Path) -> Studio:
    s = Studio.for_bundle(tmp_path, bundle_name="t")
    s.ensure_session()
    return s


def test_for_configured_bundle_uses_custom_session_dir(tmp_path: Path) -> None:
    """Configured studio.session_dir is the single session-state owner."""
    (tmp_path / "okf-loom.config.yaml").write_text(
        "studio:\n  session_dir: .okf-loom/custom-session\n",
        encoding="utf-8",
    )

    s = Studio.for_configured_bundle(tmp_path, bundle_name="t")
    assert s.session_dir == tmp_path / ".okf-loom" / "custom-session"
    s.ensure_session()

    assert s.session_dir.is_dir()
    assert not (tmp_path / ".okf-loom" / "session").exists()


def test_append_event_stamps_id_ts_seq_rev(studio: Studio) -> None:
    e1 = studio.append_event({"type": "changed", "ids": ["a"]})
    e2 = studio.append_event({"type": "changed", "ids": ["b"]})
    for e in (e1, e2):
        assert "id" in e and "ts" in e and "seq" in e and "rev" in e
    assert e2["seq"] > e1["seq"]
    assert e2["rev"] > e1["rev"]
    # Persisted to events.jsonl.
    rows = list(studio.read_events(limit=10))
    assert len(rows) == 2


def test_append_event_after_rotation_lands_in_active_jsonl(studio: Studio) -> None:
    """The first post-rotation record belongs in the fresh active feed."""
    studio.events_max_bytes = 1
    studio.events_keep = 5

    first = studio.append_event({"type": "changed", "ids": ["before"]})
    second = studio.append_event({"type": "changed", "ids": ["after"]})

    rotated = list(studio.session_dir.glob("events-*-*[0-9].jsonl"))
    assert rotated, "expected events.jsonl to rotate"
    assert studio.events_path.is_file(), "post-rotation append must recreate active feed"
    active_rows = [
        json.loads(line) for line in studio.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["id"] for row in active_rows] == [second["id"]]
    assert all(first["id"] != row.get("id") for row in active_rows)


def test_read_events_filters_by_actor_concept_since(studio: Studio) -> None:
    studio.record_activity(actor="agent", action="add_link", ids=["tables/orders"], summary="x")
    studio.record_activity(actor="cli", action="repair", ids=["tables/users"], summary="y")
    assert len(studio.read_events(actor="agent")) == 1
    # §7.2 graph event emission (P1-6): a graph-affecting action (add_link,
    # repair, …) emits a follow-on ``graph`` event with the same ``ids`` so
    # the graph view invalidates its layout. Both events match the concept
    # filter, so the count is 2 (activity + graph).
    matched = studio.read_events(concept="tables/users")
    assert {e["type"] for e in matched} == {"activity", "graph"}
    assert len(matched) == 2
    all_rows = studio.read_events(limit=20)
    after = studio.read_events(since=all_rows[0]["id"], limit=20)
    assert after[0]["id"] == all_rows[1]["id"]


def test_record_activity_carries_group_and_detail(studio: Studio) -> None:
    e = studio.record_activity(
        actor="agent", action="add_link", ids=["a"], summary="s",
        undoable=True, group_id="G1", detail={"target": "b"},
    )
    assert e["group_id"] == "G1" and e["undoable"] is True
    assert e["detail"] == {"target": "b"}


# ---------------------------------------------------------------------------
# Studio — comments / directives
# ---------------------------------------------------------------------------

def test_comment_lifecycle_open_claim_resolve(studio: Studio) -> None:
    c = studio.post_comment(concept="tables/orders", body="link to customers",
                            anchor={"kind": "section", "ref": "#schema"})
    assert c["state"] == "open" and c["claimed_by"] is None
    cid = c["id"]

    claimed = studio.update_comment(cid, state="claimed", claimed_by="agent")
    assert claimed["state"] == "claimed" and claimed["claimed_by"] == "agent"

    resolved = studio.update_comment(cid, state="resolved", resolved_activity=[12],
                                     reply="Done — added the link.")
    assert resolved["state"] == "resolved"
    assert resolved["resolved_activity"] == [12]
    assert resolved["reply"] == "Done — added the link."

    # get_comment returns the LATEST record per id (append-only feed).
    assert studio.get_comment(cid)["state"] == "resolved"


def test_list_comments_dedupes_to_latest_per_id(studio: Studio) -> None:
    c = studio.post_comment(concept="a", body="one")
    studio.update_comment(c["id"], state="resolved")
    c2 = studio.post_comment(concept="b", body="two")
    # Two distinct ids, each at its latest state.
    allc = {x["id"]: x for x in studio.list_comments()}
    assert len(allc) == 2
    assert allc[c["id"]]["state"] == "resolved"
    assert allc[c2["id"]]["state"] == "open"
    assert studio.list_comments(state="resolved")[0]["id"] == c["id"]
    assert studio.list_comments(state="open")[0]["id"] == c2["id"]


def test_comments_remain_current_after_directive_rotation(studio: Studio) -> None:
    c = studio.post_comment(concept="tables/orders", body="link to customers")
    # Force the creation record into a rotated directives file before reading
    # or updating it. This mirrors a long-running Studio session whose comment
    # state has crossed the directive rotation cap.
    assert studio.directives_path.stat().st_size >= 1
    from okf_loom import studio as studio_mod

    studio_mod._rotate_jsonl(studio.directives_path, keep=3)
    assert not studio.directives_path.exists()

    assert [row["id"] for row in studio.list_comments(state="open")] == [c["id"]]
    assert studio.get_comment(c["id"])["state"] == "open"
    work = wait_for_work(studio.bundle_root, kinds=("comment",), timeout=0.1, interval=0.01)
    assert work is not None
    assert work["id"] == c["id"]

    claimed = studio.update_comment(c["id"], state="claimed", claimed_by="agent")
    assert claimed is not None
    assert claimed["state"] == "claimed"
    assert studio.directives_path.is_file()
    assert studio.get_comment(c["id"])["state"] == "claimed"
    assert studio.list_comments(state="claimed")[0]["id"] == c["id"]


def test_comment_for_unknown_id_returns_none(studio: Studio) -> None:
    assert studio.get_comment("nope") is None
    assert studio.update_comment("nope", state="resolved") is None


# ---------------------------------------------------------------------------
# Studio — presence
# ---------------------------------------------------------------------------

def test_presence_set_and_get(studio: Studio) -> None:
    p = studio.set_presence(actor="agent", state="editing", focus="tables/orders")
    assert p["state"] == "editing" and p["focus"] == "tables/orders"
    got = studio.get_presence()
    assert got["state"] == "editing"


# ---------------------------------------------------------------------------
# Studio — undo (individual + group)
# ---------------------------------------------------------------------------

def test_undo_snapshot_round_trip(studio: Studio) -> None:
    rev = studio.snapshot_for_undo(concept_id="tables/orders", raw="old body text")
    assert rev == rev_of("old body text")
    assert studio.undo_snapshot(concept_id="tables/orders", rev=rev) == "old body text"


def test_external_history_substitution_fails_closed_without_outside_write_or_fd_leak(
    monkeypatch, tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir(mode=0o700)
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    outside = tmp_path / "outside"; outside.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(external))
    # Ownership is proven per descriptor by kernel identity (fd, device,
    # inode), never by a whole-process descriptor count: unrelated Studios
    # legitimately finalize during this test and move the process-wide
    # number without leaking anything of ours. This unrelated sentinel is
    # retained for the whole test; it must stay open, with one stable
    # identity, no matter which other owners come and go.
    sentinel_path = tmp_path / "unrelated-sentinel.txt"
    sentinel = sentinel_path.open("w")
    sentinel_identity = _fd_identity(sentinel.fileno())

    real_close = os.close
    observed_closes: list[tuple[int, int, int]] = []

    def recording_close(fd):
        observed_closes.append(_fd_identity(fd))
        return real_close(fd)

    monkeypatch.setattr(os, "close", recording_close)

    def authority_descriptors(studio: Studio) -> list:
        return [authority for authority in (
            studio._bundle_authority, studio.session_authority,
            *studio._session_descendants.values(),
        ) if authority is not None]

    def open_identities(authorities: list) -> set[tuple[int, int, int]]:
        identities = set()
        for authority in authorities:
            if authority.fd >= 0:
                identities.add(_fd_identity(authority.fd))
            if authority.parent_fd >= 0:
                identities.add(_fd_identity(authority.parent_fd))
        return identities

    studio = Studio.for_configured_bundle(bundle)
    try:
        studio.ensure_session()
        history = external / "history"
        assert stat.S_IMODE(history.stat().st_mode) == 0o700
        shutil.rmtree(history)
        history.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            studio.snapshot_for_undo(concept_id="tables/orders", raw="secret")
        assert list(outside.iterdir()) == []

        # Capture the invocation-owned descriptor identities while live.
        authorities = authority_descriptors(studio)
        owned_ids = open_identities(authorities)
        assert owned_ids, "the invocation must own live descriptors before close"

        # An unrelated Studio session that this test retains, then closes
        # inside the same observation window: exactly the unrelated
        # finalization that once moved the whole-process count. It must
        # neither close our descriptors nor fool the ownership oracle.
        unrelated_bundle = tmp_path / "unrelated-bundle"
        unrelated_bundle.mkdir(mode=0o700)
        unrelated = Studio.for_bundle(unrelated_bundle)
        try:
            unrelated.ensure_session()
            unrelated_ids = open_identities(authority_descriptors(unrelated))
            assert unrelated_ids
        finally:
            unrelated.close()

        studio.close()

        # Every owned kernel descriptor was closed by its owner, and every
        # authority records the closed state.
        assert owned_ids <= set(observed_closes), (
            "Studio.close must close each owned kernel descriptor")
        assert all(authority.fd == authority.parent_fd == -1
                   for authority in authorities)
        # The unrelated finalization was observed as its own closes and left
        # no trace on the tested ownership set.
        assert unrelated_ids <= set(observed_closes)
        # The sentinel was never closed by either owner and keeps its exact
        # identity: same fd, device and inode, still usable.
        assert sentinel_identity not in observed_closes
        assert _fd_identity(sentinel.fileno()) == sentinel_identity
        # No owned descriptor identity survives anywhere in the process.
        survivors = []
        for entry in os.listdir("/proc/self/fd"):
            try:
                candidate = _fd_identity(int(entry))
            except OSError:
                continue  # vanished between listing and stat: closed
            if candidate in owned_ids:
                survivors.append(candidate)
        assert survivors == []
    finally:
        studio.close()
        sentinel.close()


def _fd_identity(fd: int) -> tuple[int, int, int]:
    info = os.fstat(fd)
    return fd, info.st_dev, info.st_ino


@pytest.mark.parametrize("anchor_change", ["replace", "remove", "rename"])
def test_external_authority_anchor_mutation_cannot_redirect_studio_state(
    monkeypatch, tmp_path: Path, anchor_change: str,
) -> None:
    from okf_loom.studio import _configured_session_root

    bundle = tmp_path / "bundle"; bundle.mkdir(mode=0o700)
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    outside = tmp_path / "outside"; outside.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(external))
    baseline = len(os.listdir("/proc/self/fd"))
    anchor = external / ".okf-authority-anchor"
    anchor.mkdir(mode=0o700)  # legacy authority admitted before this fix
    authority = _configured_session_root(bundle, ".okf-loom/session")
    if anchor_change == "rename":
        anchor.rename(external / ".okf-authority-anchor-renamed")
    else:
        anchor.rmdir()
    if anchor_change == "replace":
        anchor.symlink_to(outside, target_is_directory=True)

    studio = Studio.for_bundle(
        bundle, session_rel=str(external), session_authority=authority,
        constraints={"propose_only": True},
    )
    try:
        studio.ensure_session()
        studio.append_event({"type": "activity", "concept": "x"})
        comment = studio.post_comment(concept="x", body="comment", anchor={})
        studio.post_comment_idempotent(
            request={"concept": "y", "body": "wal"},
            idempotency_key="ANCHOR-RACE-WAL", concept="y", body="wal",
            anchor={}, actor="agent", detail={},
        )
        rev = studio.snapshot_for_undo(concept_id="x", raw="history")
        assert studio.undo_snapshot(concept_id="x", rev=rev) == "history"
        assert studio.get_comment(comment["id"])["body"] == "comment"
        assert studio.post_suggestion(
            concept="x", action="replace_text", args={}, summary="proposal",
        )["state"] == "open"
    finally:
        studio.close()

    assert list(outside.iterdir()) == []
    assert (external / "events.jsonl").is_file()
    assert (external / "proposals.jsonl").is_file()
    assert len(os.listdir("/proc/self/fd")) == baseline


def test_bundle_local_session_parent_substitution_keeps_real_operations_pinned(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    baseline = len(os.listdir("/proc/self/fd"))
    studio = Studio.for_bundle(bundle, constraints={"propose_only": True})
    try:
        studio.ensure_session()
        session = studio.session_dir
        pinned = session.with_name("session-pinned")
        session.rename(pinned)
        session.symlink_to(outside, target_is_directory=True)

        studio.append_event({"type": "activity", "concept": "x"})
        comment = studio.post_comment(concept="x", body="comment", anchor={})
        rev = studio.snapshot_for_undo(concept_id="x", raw="history")
        studio.idempotency_store("KEY-A", body={"x": 1}, response={"ok": True})
        studio.post_suggestion(
            concept="x", action="replace_text", args={}, summary="proposal")

        assert studio.get_comment(comment["id"])["body"] == "comment"
        assert studio.undo_snapshot(concept_id="x", rev=rev) == "history"
        assert studio.idempotency_lookup("KEY-A", body={"x": 1}) == {"ok": True}
        assert list(outside.iterdir()) == []
        assert (pinned / "events.jsonl").is_file()
        assert (pinned / "proposals.jsonl").is_file()
    finally:
        studio.close()
    assert len(os.listdir("/proc/self/fd")) == baseline


def test_fresh_bundle_parent_substitution_before_first_write_uses_pinned_root(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"; parent.mkdir()
    bundle = parent / "bundle"; bundle.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    baseline = len(os.listdir("/proc/self/fd"))
    studio = Studio.for_bundle(bundle)
    pinned_parent = tmp_path / "parent-pinned"
    parent.rename(pinned_parent)
    parent.symlink_to(outside, target_is_directory=True)
    try:
        studio.ensure_session()
        studio.append_event({"type": "changed", "ids": ["pinned"]})
        assert (pinned_parent / "bundle/.okf-loom/session/events.jsonl").is_file()
        assert list(outside.iterdir()) == []
    finally:
        studio.close()
    assert len(os.listdir("/proc/self/fd")) == baseline


def test_fresh_okf_loom_substitution_before_first_write_fails_closed(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    workdir = bundle / ".okf-loom"; workdir.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    baseline = len(os.listdir("/proc/self/fd"))
    studio = Studio.for_bundle(bundle)
    pinned = bundle / ".okf-loom-pinned"
    workdir.rename(pinned)
    workdir.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(OSError):
            studio.ensure_session()
        assert list(outside.iterdir()) == []
    finally:
        studio.close()
    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("operation,descendant", [
    ("idempotency", ".idempotency"),
    ("comment-wal", ".comment-create-wal"),
])
def test_external_state_descendant_substitution_cannot_redirect_real_studio_operation(
    monkeypatch, tmp_path: Path, operation: str, descendant: str,
) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir(mode=0o700)
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    outside = tmp_path / "outside"; outside.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(external))
    studio = Studio.for_configured_bundle(bundle)
    try:
        studio.ensure_session()
        if operation == "idempotency":
            studio.idempotency_store("KEY-A", body={"a": 1}, response={"ok": True})
        else:
            with pytest.raises(OSError):
                studio.post_comment_idempotent(
                    request={"concept": "x", "body": "first"},
                    idempotency_key="COMMENT-KEY-A", concept="x", body="first",
                    anchor={}, actor="agent", detail={}, fault="after_prepare")
        owned = external / descendant
        assert stat.S_IMODE(owned.stat().st_mode) == 0o700
        pinned = external / f"{descendant}.owned"
        owned.rename(pinned)
        owned.symlink_to(outside, target_is_directory=True)

        if operation == "idempotency":
            studio.idempotency_store("KEY-B", body={"b": 2}, response={"ok": True})
        else:
            # The descendant authority was pinned before its display name was
            # replaced, so the second prepared WAL remains on that old inode.
            with pytest.raises(OSError):
                studio.post_comment_idempotent(
                    request={"concept": "y", "body": "second"},
                    idempotency_key="COMMENT-KEY-B", concept="y", body="second",
                    anchor={}, actor="agent", detail={}, fault="after_prepare")

        assert list(outside.iterdir()) == []
        assert stat.S_IMODE(pinned.stat().st_mode) == 0o700
    finally:
        studio.close()


def test_undo_group_manifest(studio: Studio) -> None:
    r1 = studio.snapshot_for_undo(concept_id="tables/orders", raw="a", group_id="PASS1")
    r2 = studio.snapshot_for_undo(concept_id="tables/customers", raw="b", group_id="PASS1")
    members = studio.undo_group("PASS1")
    assert members is not None
    concepts = {m["concept"] for m in members}
    assert concepts == {"tables/orders", "tables/customers"}
    # Each member restores independently.
    assert studio.undo_snapshot(concept_id="tables/orders", rev=r1) == "a"
    assert studio.undo_snapshot(concept_id="tables/customers", rev=r2) == "b"


def test_undo_unknown_group_returns_none(studio: Studio) -> None:
    assert studio.undo_group("MISSING") is None


def test_undo_unknown_snapshot_returns_none(studio: Studio) -> None:
    assert studio.undo_snapshot(concept_id="x", rev="deadbeefdead") is None


# ---------------------------------------------------------------------------
# Cross-process append (advisory lock) — basic durability check
# ---------------------------------------------------------------------------

def test_events_jsonl_is_one_record_per_line(studio: Studio, tmp_path: Path) -> None:
    import json
    studio.record_activity(actor="agent", action="add_link", ids=["a"], summary="s")
    studio.record_activity(actor="cli", action="repair", ids=["b"], summary="t")
    text = studio.events_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # §7.2 graph event emission (P1-6): each graph-affecting activity
    # (add_link, repair) is followed by a ``graph`` event, so 2 activities
    # → 4 lines (2 activities + 2 graphs).
    assert len(lines) == 4
    types = [json.loads(ln)["type"] for ln in lines]
    assert types.count("activity") == 2
    assert types.count("graph") == 2
    for ln in lines:
        obj = json.loads(ln)  # each line is valid JSON
        assert "id" in obj and "ts" in obj


# ---------------------------------------------------------------------------
# Agent foreground wait primitive (current spec §12)
# ---------------------------------------------------------------------------

def test_wait_for_work_returns_none_on_timeout(tmp_path: Path) -> None:
    """With no work and a short timeout, wait_for_work returns None promptly."""
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    start = time.time()
    result = wait_for_work(tmp_path, kinds=("comment",), timeout=1.0, interval=0.2)
    elapsed = time.time() - start
    assert result is None
    assert elapsed < 2.5  # returns ~at the timeout, not much later


@pytest.mark.parametrize("kind", ["comment", "change"])
def test_wait_fresh_bundle_is_empty_feed_and_closes_descriptors(
    tmp_path: Path, kind: str,
) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    baseline = len(os.listdir("/proc/self/fd"))
    assert wait_for_work(bundle, kinds=(kind,), timeout=0.05, interval=0.01) is None
    assert not (bundle / ".okf-loom").exists()
    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("kind", ["comment", "change"])
def test_wait_fresh_bundle_adopts_real_work_created_later(
    tmp_path: Path, kind: str,
) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    producer = Studio.for_bundle(bundle)

    def _publish() -> None:
        time.sleep(0.1)
        if kind == "comment":
            producer.post_comment(concept="x", body="wake")
        else:
            producer.emit_change(kind="changed", ids=["x"], origin="test")

    thread = threading.Thread(target=_publish)
    thread.start()
    try:
        item = wait_for_work(bundle, kinds=(kind,), timeout=2, interval=0.02)
        assert item is not None and item["kind"] == kind
    finally:
        thread.join()
        producer.close()


def test_wait_existing_session_parent_substitution_reads_pinned_inode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"; parent.mkdir()
    bundle = parent / "bundle"; bundle.mkdir()
    producer = Studio.for_bundle(bundle); producer.ensure_session()
    producer.post_comment(concept="x", body="pinned ask")
    outside = tmp_path / "outside"; outside.mkdir()
    original = Studio.for_configured_bundle.__func__

    def _construct(cls, root, **kwargs):
        studio = original(cls, root, **kwargs)
        pinned = tmp_path / "parent-pinned"
        parent.rename(pinned)
        parent.symlink_to(outside, target_is_directory=True)
        return studio

    monkeypatch.setattr(Studio, "for_configured_bundle", classmethod(_construct))
    try:
        item = wait_for_work(bundle, kinds=("comment",), timeout=0.2, interval=0.01)
        assert item is not None and item["body"] == "pinned ask"
        assert list(outside.iterdir()) == []
    finally:
        producer.close()


def test_wait_for_work_catches_a_new_comment(tmp_path: Path) -> None:
    """A comment posted during the wait is returned (the agent foreground loop)."""
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()

    def _post_later() -> None:
        time.sleep(0.4)
        studio.post_comment(concept="tables/orders", body="link to customers")

    threading.Thread(target=_post_later, daemon=True).start()
    item = wait_for_work(tmp_path, kinds=("comment",), timeout=4.0, interval=0.2)
    assert item is not None
    assert item["kind"] == "comment"
    assert item["body"] == "link to customers"
    assert item["state"] == "open"


def test_wait_for_work_reads_configured_session_dir(tmp_path: Path) -> None:
    """The foreground agent loop reads comments from studio.session_dir."""
    (tmp_path / "okf-loom.config.yaml").write_text(
        "studio:\n  session_dir: .okf-loom/custom-session\n",
        encoding="utf-8",
    )
    studio = Studio.for_configured_bundle(tmp_path)
    studio.ensure_session()
    created = studio.post_comment(concept="tables/orders", body="please enrich")

    item = wait_for_work(tmp_path, kinds=("comment",), timeout=0.2, interval=0.01)

    assert item is not None and item["kind"] == "comment"
    assert item["id"] == created["id"]
    assert studio.directives_path.is_file()
    assert not (tmp_path / ".okf-loom" / "session" / "directives.jsonl").exists()


def test_wait_for_work_drains_pre_existing_open_comments(tmp_path: Path) -> None:
    """Pre-existing open comments ARE returned on the first call (backlog drain).

    Previously the baseline was set to the latest comment id, so pre-existing
    open comments were skipped. Now they're returned immediately so the agent
    picks up work posted before it started. Pass ``--since`` to skip the
    backlog and only wait for truly new work.
    """
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    studio.post_comment(concept="a", body="old")  # pre-existing
    # Without --since: the pre-existing comment IS returned (backlog drain).
    item = wait_for_work(tmp_path, kinds=("comment",), timeout=1.0, interval=0.2)
    assert item is not None
    assert item["body"] == "old"
    assert item["kind"] == "comment"


def test_wait_for_work_since_skips_backlog(tmp_path: Path) -> None:
    """With --since pinned, pre-existing comments are NOT returned."""
    from okf_loom.studio import _read_jsonl
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    studio.post_comment(concept="a", body="old")  # pre-existing
    # With a since that's after the pre-existing comment: skip it.
    latest = None
    for row in _read_jsonl(studio.directives_path):
        if row.get("id"):
            latest = row["id"]
    item = wait_for_work(
        tmp_path, kinds=("comment",), since=latest,
        timeout=1.0, interval=0.2,
    )
    assert item is None  # nothing newer than the baseline


# ---------------------------------------------------------------------------
# Review-fix regression tests (F3 / F8 / F14)
# ---------------------------------------------------------------------------


def test_disk_event_dedupes_when_rev_matches_logged(studio: Studio, tmp_path: Path) -> None:
    """F3 (§10.5): a disk-origin ``emit_change`` whose on-disk content rev
    matches the rev a mutator just logged (via ``mark_logged``) is de-duped —
    it is NOT appended/published. A genuinely different rev is still logged."""
    cid = "x"
    raw_r = "---\ntype: t\n---\n# X\nbody-r\n"
    # Put the content on disk so emit_change's on-disk hash == rev_of(raw_r).
    (tmp_path / "x.md").write_text(raw_r, encoding="utf-8")
    rev_r = rev_of(raw_r)
    # Simulate _handle_apply: the mutator wrote raw_r and marks it logged.
    studio.mark_logged(cid, raw_r)
    assert studio._last_logged_rev[cid] == rev_r
    # The mutator emit itself is logged (NOT deduped — only disk dedups).
    studio.emit_change(kind="changed", ids=[cid], origin="mutator")
    # The watcher later sees the same mtime/content and emits a disk changed
    # for the SAME rev → must be de-duped (already logged by the mutator).
    ret = studio.emit_change(kind="changed", ids=[cid], origin="disk")
    assert ret.get("deduped") is True
    # Exactly ONE event for cid reached the feed (the mutator one).
    rows = [e for e in studio.read_events(limit=50) if cid in (e.get("ids") or [])]
    assert len(rows) == 1
    assert rows[0]["origin"] == "mutator"

    # A DIFFERENT on-disk rev is NOT deduped — it's a real new change.
    raw_r2 = "---\ntype: t\n---\n# X\nbody-r2-different\n"
    (tmp_path / "x.md").write_text(raw_r2, encoding="utf-8")
    ret2 = studio.emit_change(kind="changed", ids=[cid], origin="disk")
    assert ret2.get("deduped") is not True
    rows2 = [e for e in studio.read_events(limit=50) if cid in (e.get("ids") or [])]
    assert len(rows2) == 2


def test_disk_dedup_partial_keeps_unmatched_ids(studio: Studio, tmp_path: Path) -> None:
    """F3: when SOME ids of a disk event are deduped and others are not, the
    unmatched ids are still emitted (per-id dedup, not all-or-nothing)."""
    (tmp_path / "a.md").write_text("---\ntype: t\n---\nA\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("---\ntype: t\n---\nB\n", encoding="utf-8")
    studio.mark_logged("a", (tmp_path / "a.md").read_bytes())  # a is logged
    # b is NOT logged → must survive; a is deduped.
    ret = studio.emit_change(kind="changed", ids=["a", "b"], origin="disk")
    assert ret.get("deduped") is not True
    assert ret["ids"] == ["b"]


def test_undo_snapshot_encoding_is_injective(studio: Studio) -> None:
    """F8: snapshot_for_undo's filename encoding must be injective — ``a__b``
    and ``a/b`` map to DISTINCT history dirs. The old ``replace("/", "__")``
    collided them, so one concept's snapshot could be read as another's."""
    r1 = studio.snapshot_for_undo(concept_id="a__b", raw="content-one")
    r2 = studio.snapshot_for_undo(concept_id="a/b", raw="content-two")
    dir1 = studio.history_dir / "a__b".encode("utf-8").hex()
    dir2 = studio.history_dir / "a/b".encode("utf-8").hex()
    assert dir1 != dir2  # the old encoding made these identical
    assert dir1.is_dir() and dir2.is_dir()
    # Each concept restores its OWN bytes — no cross-contamination.
    assert studio.undo_snapshot(concept_id="a__b", rev=r1) == "content-one"
    assert studio.undo_snapshot(concept_id="a/b", rev=r2) == "content-two"


def test_now_iso_ms_consistent_with_seconds(monkeypatch, studio: Studio) -> None:
    """F14: _now_iso derives the seconds field AND the millisecond fraction
    from a SINGLE ``time.time()`` read, so they can never be inconsistent
    (e.g. ``...:59.000`` when two reads straddled a whole-second boundary)."""
    import okf_loom.studio as studio_mod
    import time as _time
    fixed = 1_700_000_000.234  # a known instant with a 234 ms fraction
    monkeypatch.setattr(studio_mod.time, "time", lambda: fixed)
    iso = studio_mod._now_iso()
    assert iso.endswith(".234Z")
    # The seconds portion must match gmtime of the SAME instant (single read).
    expected_seconds = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime(fixed))
    assert iso == expected_seconds + ".234Z"


# ---------------------------------------------------------------------------
# P2-18 — wait_for_work re-opens when a resolved comment is re-opened
# ---------------------------------------------------------------------------
# A comment that is open → resolved → re-opened (state flips back to "open")
# MUST trigger wait_for_work again. Without this, an agent that reopens a
# resolved ask (e.g. the user changed their mind) would never wake up.
def test_wait_for_work_re_fires_when_a_resolved_comment_is_reopened(
    tmp_path: Path,
) -> None:
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()

    # Pre-existing open comment.
    c1 = studio.post_comment(concept="a", body="first ask")
    # Resolve it (so a fresh wait_for_work call sees no open work).
    studio.resolve_comment(c1["id"], reply="done")
    item0 = wait_for_work(tmp_path, kinds=("comment",), timeout=0.5, interval=0.1)
    assert item0 is None, "no open work after resolve; wait should time out"

    # Start wait_for_work in a background thread (baseline captured now:
    # base_comment_ts == the resolve record's ts).
    captured: list = []

    def _wait():
        captured.append(
            wait_for_work(tmp_path, kinds=("comment",), timeout=4.0, interval=0.1)
        )

    t = threading.Thread(target=_wait, daemon=True)
    t.start()

    # After the baseline is established, re-open the comment.
    time.sleep(0.3)  # let wait_for_work capture its baseline
    studio.update_comment(c1["id"], state="open")  # state flip → new ts

    t.join(timeout=5.0)
    assert not t.is_alive(), "wait_for_work did not return after the re-open"
    item = captured[0]
    assert item is not None, "wait_for_work timed out after the re-open"
    assert item["id"] == c1["id"]
    assert item["state"] == "open", (
        f"re-opened comment should surface as state=open; got {item['state']!r}"
    )
