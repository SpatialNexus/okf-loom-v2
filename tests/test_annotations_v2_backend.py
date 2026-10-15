import threading
import json
import multiprocessing
import os
import stat
import hashlib
from pathlib import Path

import pytest

from scripts.okf_loom.annotations import capture_anchor, concept_content_rev, resolve_anchor
from scripts.okf_loom.directive_store import DirectiveStore, DirectiveStoreError
from scripts.okf_loom.studio import CommentMutationError, Studio


def _reanchor_worker(root, comment_id, token, source, anchor, key, gate, conn):
    studio = Studio.for_bundle(root)
    gate.wait()
    try:
        result = studio.reanchor_comment(
            comment_id=comment_id, expected_row_token=token,
            expected_concept_content_rev=concept_content_rev(source), anchor=anchor,
            idempotency_key=key, canonical_source=source,
        )
        conn.send(("ok", result[0]["comment"]["id"]))
    except Exception as exc:
        conn.send(("error", getattr(exc, "code", type(exc).__name__)))
    finally:
        conn.close()


def _event_worker(root, gate, conn):
    studio = Studio.for_bundle(root)
    gate.wait()
    event = studio.append_event({"type": "comment", "id": "SAME"})
    conn.send(event["event_cursor"])
    conn.close()


def physical_rows(studio):
    paths = sorted(studio.session_dir.glob("directives-*-*.jsonl")) + [studio.directives_path]
    return [json.loads(line) for path in paths if path.is_file()
            for line in path.read_text(encoding="utf-8").splitlines() if line]


def _directive_reader(root, gate, conn):
    studio = Studio.for_bundle(root)
    gate.wait()
    conn.send(sorted(row["id"] for row in studio.list_comments()))
    conn.close()


def _directive_writer(root, gate, conn):
    studio = Studio.for_bundle(root)
    gate.wait()
    conn.send(studio.post_comment(concept="note", body="new")["id"])
    conn.close()


def _create_comment_worker(root, request, key, gate, conn):
    studio = Studio.for_bundle(root); gate.wait()
    try:
        row, replayed = studio.post_comment_idempotent(
            request=request, idempotency_key=key, concept=request["concept"],
            body=request["body"], anchor=request.get("anchor", {}), actor="user",
            detail={}, parent_id=None,
        )
        conn.send(("ok", row["id"], replayed))
    except Exception as exc:
        conn.send(("error", getattr(exc, "code", type(exc).__name__), False))
    conn.close()


def test_full_row_writers_preserve_unknown_fields_and_use_fresh_tokens(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    comment = studio.post_comment(concept="note", body="hello", detail={"future": True})
    first = comment["row_token"]
    updated = studio.update_comment(comment["id"], state="claimed", claimed_by="agent")
    assert updated["row_token"] != first
    assert updated["detail"] == {"future": True}


def test_update_comment_absent_ids_return_none_without_lock_path(tmp_path, monkeypatch):
    studio = Studio.for_bundle(tmp_path)
    called = []
    original = studio._comment_lock_path
    monkeypatch.setattr(studio, "_comment_lock_path",
                        lambda value: (called.append(value), original(value))[1])
    assert studio.update_comment("../../unsafe", state="resolved") is None
    assert studio.update_comment("01JABED9K0", state="resolved") is None
    assert called == []
    assert not studio.session_dir.exists()
    present = studio.post_comment(concept="note", body="present")
    called.clear()
    updated = studio.update_comment(present["id"], state="resolved")
    assert updated["state"] == "resolved"
    assert called == [present["id"]]


def test_concurrent_lifecycle_writers_merge_owned_fields_under_one_lock(tmp_path):
    studio_a = Studio.for_bundle(tmp_path)
    studio_b = Studio.for_bundle(tmp_path)
    comment = studio_a.post_comment(concept="note", body="hello", anchor={"future": 1})
    baseline_events = len([event for event in studio_a.read_event_snapshot()
                           if event.get("type") == "comment"
                           and event.get("id") == comment["id"]])
    barrier = threading.Barrier(2)
    errors = []

    def claim():
        try:
            barrier.wait()
            studio_a.update_comment(comment["id"], state="claimed", claimed_by="agent")
        except BaseException as exc:
            errors.append(exc)

    def edit():
        try:
            barrier.wait()
            studio_b.update_comment(comment["id"], body="edited")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim), threading.Thread(target=edit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    latest = studio_a.get_comment(comment["id"])
    assert latest["state"] == "claimed"
    assert latest["body"] == "edited"
    assert latest["anchor"] == {"future": 1}
    retained_events = [event for event in studio_a.read_event_snapshot()
                       if event.get("type") == "comment"
                       and event.get("id") == comment["id"]]
    assert len(retained_events) == baseline_events + 2


def test_ensure_session_reuses_writer_but_rejects_reader_ownership(tmp_path, monkeypatch):
    studio = Studio.for_bundle(tmp_path)
    studio.session_dir.mkdir(parents=True)
    with DirectiveStore(studio.session_dir).initialize():
        pass
    store = DirectiveStore(studio.session_dir)
    original_initialize = DirectiveStore.initialize

    with store.writer():
        monkeypatch.setattr(
            DirectiveStore, "initialize",
            lambda self: (_ for _ in ()).throw(AssertionError("nested initialize")))
        studio.ensure_session()
    assert studio._session_initialized is True

    reader_studio = Studio.for_bundle(tmp_path)
    monkeypatch.setattr(DirectiveStore, "initialize", original_initialize)
    with store.reader():
        with pytest.raises(DirectiveStoreError, match="nested"):
            reader_studio.ensure_session()
    assert reader_studio._session_initialized is False


def test_ensure_session_owned_writer_performs_legacy_checkpoint_migration(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    studio.session_dir.mkdir(parents=True)
    row = {"id": "01JABED9K0", "concept": "note", "state": "open",
           "archived": False}
    (studio.session_dir / "..directives-transaction.okf-lock").write_bytes(b"")
    rotation = studio.session_dir / "directives-20260101-000000000001.jsonl"
    rotation.write_text(json.dumps(row) + "\n")
    studio.directives_checkpoint_path.write_text(json.dumps({
        "version": 1, "rows": [row],
    }))

    with DirectiveStore(studio.session_dir).writer():
        studio.ensure_session()

    checkpoint = json.loads(studio.directives_checkpoint_path.read_text())
    assert checkpoint["through_rotation"] == 1
    assert studio._session_initialized is True


def test_reanchor_wal_recovers_append_before_finalize_and_replays_once(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    source = "Before target after\n"
    old = studio.post_comment(concept="note", body="hello", anchor={"kind": "text", "ref": "target"})
    anchor = capture_anchor(source, source.index("target"), source.index("target") + 6)
    kwargs = dict(comment_id=old["id"], expected_row_token=old["row_token"],
                  expected_concept_content_rev=concept_content_rev(source), anchor=anchor,
                  idempotency_key="reanchor-key-001", canonical_source=source)
    with pytest.raises(OSError):
        studio.reanchor_comment(**kwargs, fault="after_append")
    response, replayed, status = studio.reanchor_comment(**kwargs)
    assert status == 200
    assert replayed is True
    assert response["comment"]["anchor"] == anchor
    rows = [row for row in studio.list_comments() if row["id"] == old["id"]]
    assert len(rows) == 1
    tx_rows = [row for row in physical_rows(studio)
               if row.get("reanchor_transaction_id")]
    assert len(tx_rows) == 1
    notifications = [json.loads(line) for path in
                     sorted(studio.session_dir.glob("events-*-*.jsonl")) + [studio.events_path]
                     if path.is_file() for line in path.read_text().splitlines()
                     if line and json.loads(line).get("reanchor_transaction_id")]
    assert len(notifications) == 1


def test_create_append_before_event_replay_delivers_exactly_once(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    request = {"concept": "note", "body": "once"}
    kwargs = dict(request=request, idempotency_key="create-event-fault-key",
                  concept="note", body="once", anchor={}, actor="user", detail={})
    with pytest.raises(OSError, match="notification pending"):
        studio.post_comment_idempotent(**kwargs, fault="after_notification_pending")
    row, replayed = studio.post_comment_idempotent(**kwargs)
    assert replayed is True
    assert studio.post_comment_idempotent(**kwargs)[0]["id"] == row["id"]
    events = [event for event in studio.read_event_snapshot()
              if event.get("comment_create_transaction_id")]
    assert len(events) == 1
    wal = next(json.loads(path.read_text()) for path in studio.comment_create_wal_dir.glob("*.json")
               if path.name not in {"tombstones.json", "tombstone-filter.json"})
    assert wal["notification_delivered"] is True


def test_create_recovery_payload_matches_normal_and_keyless_outbox_recovers(tmp_path, monkeypatch):
    studio = Studio.for_bundle(tmp_path)
    root = studio.post_comment(concept="note", body="root", state="resolved")
    normal = studio.post_comment(concept="note", body="reply", parent_id=root["id"])
    normal_event = next(event for event in studio.read_event_snapshot()
                        if event.get("id") == normal["id"])
    assert normal_event == {**studio._comment_event(normal),
                            **{key: normal_event[key] for key in ("event_id", "event_cursor", "seq", "rev")}}

    original = studio._append_event_locked
    monkeypatch.setattr(studio, "_append_event_locked",
                        lambda event: (_ for _ in ()).throw(OSError("disk fault")))
    stranded = studio.post_comment(concept="note", body="keyless")
    assert len([row for row in studio.list_comments() if row["id"] == stranded["id"]]) == 1
    monkeypatch.setattr(studio, "_append_event_locked", original)
    restarted = Studio.for_bundle(tmp_path)
    recovered = [event for event in restarted.read_event_snapshot()
                 if event.get("id") == stranded["id"]]
    assert len(recovered) == 1
    for key, value in restarted._comment_event(stranded).items():
        assert recovered[0][key] == value


def test_finalized_create_reconciles_notification_before_wal_retirement(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    request = {"concept": "note", "body": "retire pending"}
    kwargs = dict(request=request, idempotency_key="retire-pending-key",
                  concept="note", body="retire pending", anchor={}, actor="user", detail={})
    with pytest.raises(OSError, match="after finalize"):
        studio.post_comment_idempotent(**kwargs, fault="after_finalize")
    studio.COMMENT_CREATE_WAL_CAP = 0
    studio._sweep_comment_create_wal_locked(10**12)
    events = [event for event in studio.read_event_snapshot()
              if event.get("comment_create_transaction_id")]
    assert len(events) == 1


@pytest.mark.parametrize("operation", ["create", "reanchor"])
def test_sweep_reconciles_pending_notification_before_cap_or_expiry_retirement(operation, tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    if operation == "create":
        request = {"concept": "note", "body": "pending sweep"}
        with pytest.raises(OSError, match="notification pending"):
            studio.post_comment_idempotent(
                request=request, idempotency_key="create-pending-sweep",
                concept="note", body="pending sweep", anchor={}, actor="user", detail={},
                fault="after_notification_pending")
        studio.COMMENT_CREATE_WAL_CAP = 0
        studio._sweep_comment_create_wal_locked(10**12)
        field = "comment_create_transaction_id"
    else:
        row = studio.post_comment(concept="note", body="pending sweep")
        with pytest.raises(OSError, match="notification pending"):
            studio.reanchor_comment(
                comment_id=row["id"], expected_row_token=row["row_token"],
                expected_concept_content_rev=concept_content_rev(source),
                anchor=capture_anchor(source, 0, 6),
                idempotency_key="reanchor-pending-sweep", canonical_source=source,
                fault="after_notification_pending")
        studio.REANCHOR_WAL_CAP = 0
        studio._sweep_reanchor_wal_locked(now=10**12)
        field = "reanchor_transaction_id"
    assert sum(bool(event.get(field)) for event in studio.read_event_snapshot()) == 1
    assert not [path for path in studio.comment_create_wal_dir.glob("*.json")
                if path.name not in {"tombstones.json", "tombstone-filter.json"}]


def test_reanchor_rejects_locked_concept_identity_change(tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    row = studio.post_comment(concept="note", body="x")
    with pytest.raises(CommentMutationError, match="comment_conflict"):
        studio.reanchor_comment(
            comment_id=row["id"], expected_row_token=row["row_token"],
            expected_concept_content_rev=concept_content_rev(source),
            anchor=capture_anchor(source, 0, 6), idempotency_key="concept-race-key",
            canonical_source=source, expected_concept="other")
    assert studio.get_comment(row["id"])["anchor"] == {}


def test_create_reanchor_then_create_replay_and_unknown_prefix_extensions_survive(tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    request = {"concept": "note", "body": "once"}
    created, _ = studio.post_comment_idempotent(
        request=request, idempotency_key="create-reanchor-replay", concept="note",
        body="once", anchor={}, actor="user", detail={})
    # Future extension keys sharing transaction prefixes are stored data, not
    # transaction identity, and every lifecycle writer must round-trip them.
    current = studio.get_comment(created["id"])
    for name in ("comment_create_transaction_id", "comment_create_idempotency_key",
                 "comment_create_request_fingerprint"):
        current.pop(name, None)
    current["comment_create_future_extension"] = {"keep": 1}
    current["reanchor_future_extension"] = {"keep": 2}
    with studio.directives_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(current) + "\n")
    current = studio.get_comment(created["id"])
    studio.reanchor_comment(
        comment_id=created["id"], expected_row_token=current["row_token"],
        expected_concept_content_rev=concept_content_rev(source),
        anchor=capture_anchor(source, 0, 6), idempotency_key="reanchor-after-create",
        canonical_source=source)
    replay, replayed = studio.post_comment_idempotent(
        request=request, idempotency_key="create-reanchor-replay", concept="note",
        body="once", anchor={}, actor="user", detail={})
    assert replayed and replay["id"] == created["id"]
    latest = studio.update_comment(created["id"], state="claimed")
    assert latest["comment_create_future_extension"] == {"keep": 1}
    assert latest["reanchor_future_extension"] == {"keep": 2}
    assert len([row for row in physical_rows(studio)
                if row.get("comment_create_transaction_id")]) == 1


def test_reanchor_idempotency_different_body_conflicts(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    source = "target other\n"
    old = studio.post_comment(concept="note", body="hello")
    anchor = capture_anchor(source, 0, 6)
    kwargs = dict(comment_id=old["id"], expected_row_token=old["row_token"],
                  expected_concept_content_rev=concept_content_rev(source), anchor=anchor,
                  idempotency_key="reanchor-key-002", canonical_source=source)
    studio.reanchor_comment(**kwargs)
    changed = dict(anchor)
    changed["prefix"] = "x"
    with pytest.raises(CommentMutationError, match="idempotency_conflict"):
        studio.reanchor_comment(**{**kwargs, "anchor": changed})


def test_event_cursors_are_distinct_numeric_across_studio_instances(tmp_path):
    a = Studio.for_bundle(tmp_path)
    b = Studio.for_bundle(tmp_path)
    events = [a.append_event({"type": "comment", "id": "A"}),
              b.append_event({"type": "comment", "id": "A"}),
              a.append_event({"type": "comment", "id": "A"})]
    cursors = [event["event_cursor"] for event in events]
    assert cursors == sorted(set(cursors))
    assert all(isinstance(cursor, int) for cursor in cursors)


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_cross_process_same_key_different_comment_mutates_once(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    source = "target\n"
    anchor = capture_anchor(source, 0, 6)
    comments = [studio.post_comment(concept="note", body=str(i)) for i in range(2)]
    ctx = multiprocessing.get_context("spawn")
    gate = ctx.Event()
    pipes = [ctx.Pipe(False) for _ in range(2)]
    processes = [ctx.Process(target=_reanchor_worker, args=(
        tmp_path, comments[i]["id"], comments[i]["row_token"], source, anchor,
        "shared-key-0001", gate, pipes[i][1])) for i in range(2)]
    for process in processes:
        process.start()
    gate.set()
    outcomes = [pipe[0].recv() for pipe in pipes]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
    assert [value for kind, value in outcomes if kind == "error"] == ["idempotency_conflict"]
    latest = [studio.get_comment(row["id"])["anchor"].get("schema") for row in comments]
    assert latest.count(2) == 1


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_spawn_same_comment_same_key_body_one_physical_transaction_and_restart(tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    comment = studio.post_comment(concept="note", body="x")
    anchor = capture_anchor(source, 0, 6); key = "spawn-same-key-1"
    ctx = multiprocessing.get_context("spawn"); gate = ctx.Event()
    pipes = [ctx.Pipe(False) for _ in range(2)]
    processes = [ctx.Process(target=_reanchor_worker, args=(
        tmp_path, comment["id"], comment["row_token"], source, anchor, key, gate, child
    )) for _, child in pipes]
    for process in processes: process.start()
    gate.set(); outcomes = [parent.recv() for parent, _ in pipes]
    for process in processes:
        process.join(10); assert process.exitcode == 0
    assert outcomes == [("ok", comment["id"]), ("ok", comment["id"])] or outcomes[::-1] == [("ok", comment["id"]), ("ok", comment["id"])]
    tx_rows = [row for row in physical_rows(studio) if row.get("reanchor_idempotency_key") == key]
    assert len(tx_rows) == 1
    events = studio.read_event_snapshot()
    assert sum(row.get("reanchor_transaction_id") == tx_rows[0]["reanchor_transaction_id"] for row in events) == 1
    wal_files = [path for path in studio.reanchor_wal_dir.glob("*.json")
                 if path not in {studio.reanchor_tombstone_path, studio.reanchor_filter_path}]
    assert len(wal_files) == 1
    restarted = Studio.for_bundle(tmp_path)
    response, replayed, status = restarted.reanchor_comment(
        comment_id=comment["id"], expected_row_token=comment["row_token"],
        expected_concept_content_rev=concept_content_rev(source), anchor=anchor,
        idempotency_key=key, canonical_source=source,
    )
    assert status == 200 and replayed and response["comment"]["id"] == comment["id"]


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_spawn_public_create_same_key_restart_and_different_body_conflict(tmp_path):
    request = {"concept": "note", "body": "create once"}; key = "create-spawn-key"
    ctx = multiprocessing.get_context("spawn"); gate = ctx.Event()
    pipes = [ctx.Pipe(False) for _ in range(2)]
    processes = [ctx.Process(target=_create_comment_worker,
                             args=(tmp_path, request, key, gate, child)) for _, child in pipes]
    [process.start() for process in processes]; gate.set()
    outcomes = [parent.recv() for parent, _ in pipes]
    for process in processes: process.join(10); assert process.exitcode == 0
    assert {outcome[0] for outcome in outcomes} == {"ok"}
    assert len({outcome[1] for outcome in outcomes}) == 1
    studio = Studio.for_bundle(tmp_path)
    rows = [row for row in physical_rows(studio) if row.get("comment_create_idempotency_key") == key]
    assert len(rows) == 1
    txid = rows[0]["comment_create_transaction_id"]
    assert sum(event.get("comment_create_transaction_id") == txid
               for event in studio.read_event_snapshot()) == 1
    assert len([path for path in studio.comment_create_wal_dir.glob("*.json")
                if path not in {studio.comment_create_tombstone_path,
                                studio.comment_create_filter_path}]) == 1
    replay, replayed = studio.post_comment_idempotent(
        request=request, idempotency_key=key, concept="note", body="create once",
        anchor={}, actor="user", detail={},
    )
    assert replayed and replay["id"] == rows[0]["id"]
    with pytest.raises(CommentMutationError, match="idempotency_conflict"):
        studio.post_comment_idempotent(
            request={**request, "body": "different"}, idempotency_key=key,
            concept="note", body="different", anchor={}, actor="user", detail={},
        )


@pytest.mark.parametrize("fault", ["after_prepare", "after_append", "after_finalize"])
def test_public_create_wal_lost_response_faults_recover_once(tmp_path, fault):
    studio = Studio.for_bundle(tmp_path); request = {"concept": "note", "body": fault}
    key = f"create-{fault}-key"
    with pytest.raises(OSError):
        studio.post_comment_idempotent(
            request=request, idempotency_key=key, concept="note", body=fault,
            anchor={}, actor="user", detail={}, fault=fault,
        )
    row, replayed = Studio.for_bundle(tmp_path).post_comment_idempotent(
        request=request, idempotency_key=key, concept="note", body=fault,
        anchor={}, actor="user", detail={},
    )
    assert replayed is True
    rows = [item for item in physical_rows(studio)
            if item.get("comment_create_idempotency_key") == key]
    assert len(rows) == 1 and rows[0]["id"] == row["id"]
    txid = rows[0]["comment_create_transaction_id"]
    assert sum(event.get("comment_create_transaction_id") == txid
               for event in studio.read_event_snapshot()) == 1


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_cross_process_event_cursor_race_restart_and_rotation(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    gate = ctx.Event()
    pipes = [ctx.Pipe(False) for _ in range(4)]
    processes = [ctx.Process(target=_event_worker, args=(tmp_path, gate, child))
                 for _, child in pipes]
    for process in processes:
        process.start()
    gate.set()
    cursors = [parent.recv() for parent, _ in pipes]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    restarted = Studio.for_bundle(tmp_path)
    restarted.events_max_bytes = 1
    next_cursor = restarted.append_event({"type": "comment", "id": "SAME"})["event_cursor"]
    assert sorted(cursors) == list(range(min(cursors), min(cursors) + 4))
    assert next_cursor == max(cursors) + 1
    assert [row["event_cursor"] for row in restarted.read_events(limit=20, order="asc")] == sorted(cursors + [next_cursor])
    replay = restarted.read_events(since=str(cursors[0]), limit=20, order="asc")
    assert [row["event_cursor"] for row in replay] == [
        value for value in sorted(cursors + [next_cursor]) if value > cursors[0]
    ]


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_cross_process_directive_rotation_snapshot_and_append(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    studio.session_dir.mkdir(parents=True, exist_ok=True)
    base = {"id": "01JABED9K0", "concept": "note", "body": "x" * (2 * 1024 * 1024),
            "state": "open", "row_token": "r2:base"}
    studio.directives_path.write_text(json.dumps(base) + "\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn"); gate = ctx.Event()
    read_pipe = ctx.Pipe(False); write_pipe = ctx.Pipe(False)
    reader = ctx.Process(target=_directive_reader, args=(tmp_path, gate, read_pipe[1]))
    writer = ctx.Process(target=_directive_writer, args=(tmp_path, gate, write_pipe[1]))
    reader.start(); writer.start(); gate.set()
    seen = read_pipe[0].recv(); new_id = write_pipe[0].recv()
    reader.join(10); writer.join(10)
    assert reader.exitcode == writer.exitcode == 0
    assert base["id"] in seen
    final = {row["id"] for row in Studio.for_bundle(tmp_path).list_comments()}
    assert {base["id"], new_id} <= final
    assert any(studio.session_dir.glob("directives-*-*.jsonl"))


def test_legacy_token_and_wal_fault_expiry_cap_and_mismatch_matrix(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    source = "target\n"
    anchor = capture_anchor(source, 0, 6)
    legacy = {"id": "01JABED9K0", "concept": "note", "body": "old", "state": "open"}
    studio.session_dir.mkdir(parents=True, exist_ok=True)
    studio.directives_path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    row = studio.get_comment(legacy["id"])
    assert row["row_token"].startswith("legacy:")
    base = dict(comment_id=legacy["id"], expected_row_token=row["row_token"],
                expected_concept_content_rev=concept_content_rev(source), anchor=anchor,
                idempotency_key="fault-key-0001", canonical_source=source)
    with pytest.raises(OSError, match="before wal"):
        studio.reanchor_comment(**base, fault="before_wal")
    assert not [path for path in studio.reanchor_wal_dir.glob("*.json")
                if path not in {studio.reanchor_tombstone_path, studio.reanchor_filter_path}]
    with pytest.raises(OSError, match="after prepare"):
        studio.reanchor_comment(**base, fault="after_prepare")
    response, replayed, status = studio.reanchor_comment(**base)
    assert status == 200
    assert replayed is False and response["comment"]["id"] == legacy["id"]
    with pytest.raises(OSError, match="after finalize"):
        second_row = studio.get_comment(legacy["id"])
        studio.reanchor_comment(**{**base, "expected_row_token": second_row["row_token"],
                                   "idempotency_key": "fault-key-0002"}, fault="after_finalize")
    assert studio.reanchor_comment(**{**base,
        "expected_row_token": second_row["row_token"],
        "idempotency_key": "fault-key-0002"})[1] is True

    # Expired finalized and over-cap records become durable tombstones; a
    # retained directive proves same-body replay, while unknown outcome conflicts.
    studio.REANCHOR_WAL_CAP = 0
    studio._sweep_reanchor_wal_locked(now=10**12)
    assert studio.reanchor_tombstone_path.is_file()
    assert studio.reanchor_comment(**base)[1] is True
    assert studio.reanchor_comment(**base)[1] is True
    assert sum(1 for row in studio.read_events(limit=100)
               if row.get("reanchor_transaction_id")) == 2  # one per two distinct transactions
    tombstones = json.loads(studio.reanchor_tombstone_path.read_text())
    tombstones["uncertain-key-1"] = {"transaction_id": "missing", "fingerprint": "x",
                                      "comment_id": legacy["id"], "evicted_at": 0}
    from scripts.okf_loom.io_utils import durable_atomic_write_text
    durable_atomic_write_text(studio.reanchor_tombstone_path, json.dumps(tombstones))
    uncertain = {**base, "idempotency_key": "uncertain-key-1"}
    with pytest.raises(CommentMutationError, match="idempotency_conflict"):
        studio.reanchor_comment(**uncertain)


def test_wal_reconciliation_rejects_transaction_row_metadata_mismatch(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    source = "target\n"; anchor = capture_anchor(source, 0, 6)
    row = studio.post_comment(concept="note", body="x")
    kwargs = dict(comment_id=row["id"], expected_row_token=row["row_token"],
                  expected_concept_content_rev=concept_content_rev(source), anchor=anchor,
                  idempotency_key="mismatch-key-1", canonical_source=source)
    with pytest.raises(OSError):
        studio.reanchor_comment(**kwargs, fault="after_append")
    lines = studio.directives_path.read_text().splitlines()
    changed = json.loads(lines[-1]); changed["reanchor_request_fingerprint"] = "bad"
    lines[-1] = json.dumps(changed)
    studio.directives_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(CommentMutationError, match="idempotency_conflict"):
        studio.reanchor_comment(**kwargs)


def test_prepared_wal_retained_through_grace_then_tombstoned(tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    row = studio.post_comment(concept="note", body="x")
    kwargs = dict(comment_id=row["id"], expected_row_token=row["row_token"],
                  expected_concept_content_rev=concept_content_rev(source),
                  anchor=capture_anchor(source, 0, 6), idempotency_key="prepared-key-1",
                  canonical_source=source)
    with pytest.raises(OSError):
        studio.reanchor_comment(**kwargs, fault="after_prepare")
    wal_path = next(path for path in studio.reanchor_wal_dir.glob("*.json")
                    if path != studio.reanchor_tombstone_path)
    wal = json.loads(wal_path.read_text())
    studio.REANCHOR_WAL_CAP = 0
    studio._sweep_reanchor_wal_locked(now=wal["expires_at"] + studio.REANCHOR_PREPARED_GRACE_SECONDS - 1)
    assert wal_path.is_file()
    studio._sweep_reanchor_wal_locked(now=wal["expires_at"] + studio.REANCHOR_PREPARED_GRACE_SECONDS + 1)
    assert not wal_path.exists()
    assert "prepared-key-1" in json.loads(studio.reanchor_tombstone_path.read_text())
    with pytest.raises(CommentMutationError, match="idempotency_conflict"):
        studio.reanchor_comment(**kwargs)


def test_tombstones_compact_to_bounded_fail_closed_filter(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    studio.REANCHOR_TOMBSTONE_CAP = 1
    studio.REANCHOR_TOMBSTONE_TTL_SECONDS = 10
    studio.reanchor_wal_dir.mkdir(parents=True, exist_ok=True)
    tombstones = {f"old-key-{i}": {"transaction_id": f"tx{i}", "fingerprint": "f",
                   "comment_id": "01JABED9K0", "evicted_at": i} for i in range(4)}
    from scripts.okf_loom.io_utils import durable_atomic_write_text
    durable_atomic_write_text(studio.reanchor_tombstone_path, json.dumps(tombstones))
    studio._sweep_reanchor_wal_locked(now=100)
    remaining = json.loads(studio.reanchor_tombstone_path.read_text())
    assert len(remaining) <= 1
    assert studio.reanchor_filter_path.stat().st_size < 100_000
    assert studio._reanchor_filter_contains("old-key-0")
    studio.REANCHOR_WAL_CAP = 0
    studio._sweep_reanchor_wal_locked(now=101)
    assert studio.reanchor_filter_path.is_file()
    assert not [path for path in studio.reanchor_wal_dir.glob("*.json")
                if path not in {studio.reanchor_tombstone_path, studio.reanchor_filter_path}]


def test_post_comment_id_and_row_token_collisions_fail_closed(tmp_path, monkeypatch):
    studio = Studio.for_bundle(tmp_path)
    existing = studio.post_comment(concept="note", body="one")
    import scripts.okf_loom.studio as module
    values = iter([existing["id"], "01KX8ZZZZZ0000000000000000"])
    monkeypatch.setattr(module, "new_id", lambda: next(values))
    with pytest.raises(CommentMutationError, match="unavailable"):
        studio.post_comment(concept="note", body="collision")
    values = iter(["01KX8YYYYY0000000000000000", existing["row_token"].removeprefix("r2:")])
    monkeypatch.setattr(module, "new_id", lambda: next(values))
    with pytest.raises(CommentMutationError, match="unavailable"):
        studio.post_comment(concept="note", body="token collision")
    assert len(studio.list_comments()) == 1


def test_event_read_snapshot_blocks_during_rotation_and_directory_fsync_fault(tmp_path, monkeypatch):
    studio = Studio.for_bundle(tmp_path); studio.events_max_bytes = 1
    studio.append_event({"type": "first"})
    import scripts.okf_loom.studio as module
    entered = threading.Event(); release = threading.Event()
    original_rotate = module._rotate_jsonl

    def blocked_rotate(path, *, keep, prune=True):
        result = original_rotate(path, keep=keep, prune=prune)
        entered.set(); assert release.wait(5)
        return result

    monkeypatch.setattr(module, "_rotate_jsonl", blocked_rotate)
    writer = threading.Thread(target=lambda: studio.append_event({"type": "second"}))
    writer.start(); assert entered.wait(5)
    finished = threading.Event(); observed = []
    reader = threading.Thread(target=lambda: (observed.extend(studio.read_events(limit=10)), finished.set()))
    reader.start(); assert not finished.is_set()
    release.set(); writer.join(5); reader.join(5)
    assert [row["type"] for row in observed] == ["first", "second"]

    # A directory fsync failure after rotation is observable and no new event
    # is reported as committed.
    real_fsync = module.os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        return real_fsync(fd)
    monkeypatch.setattr(module.os, "fsync", fail_directory)
    with pytest.raises(OSError, match="directory fsync failed"):
        studio.append_event({"type": "third"})


@pytest.mark.parametrize("fault", ["rename", "rename_dir_fsync", "create", "write", "file_fsync"])
def test_rotation_fault_restart_exposes_complete_old_or_successor(tmp_path, monkeypatch, fault):
    import scripts.okf_loom.studio as module
    path = tmp_path / "events.jsonl"; lock = threading.RLock()
    module._append_jsonl(path, {"index": 0}, proc_lock=lock)
    real_rename, real_open, real_write, real_fsync = module.os.rename, module.os.open, module.os.write, module.os.fsync
    real_dirsync = module._fsync_directory
    if fault == "rename":
        monkeypatch.setattr(module.os, "rename", lambda *_: (_ for _ in ()).throw(OSError("rename")))
    elif fault == "rename_dir_fsync":
        monkeypatch.setattr(module, "_fsync_directory", lambda *_: (_ for _ in ()).throw(OSError("dirsync")))
    elif fault == "create":
        monkeypatch.setattr(module.os, "open", lambda target, flags, *a, **kw:
                            (_ for _ in ()).throw(OSError("create")) if Path(target) == path and flags & os.O_WRONLY
                            else real_open(target, flags, *a, **kw))
    elif fault == "write":
        monkeypatch.setattr(module.os, "write", lambda *_: (_ for _ in ()).throw(OSError("write")))
    elif fault == "file_fsync":
        monkeypatch.setattr(module.os, "fsync", lambda fd:
                            (_ for _ in ()).throw(OSError("fsync")) if stat.S_ISREG(os.fstat(fd).st_mode)
                            else real_fsync(fd))
    with pytest.raises(OSError):
        module._append_jsonl(path, {"index": 1}, proc_lock=lock, max_bytes=1, keep=3)
    monkeypatch.setattr(module.os, "rename", real_rename)
    monkeypatch.setattr(module.os, "open", real_open)
    monkeypatch.setattr(module.os, "write", real_write)
    monkeypatch.setattr(module.os, "fsync", real_fsync)
    monkeypatch.setattr(module, "_fsync_directory", real_dirsync)
    rows = list(module._read_jsonl_with_rotations(path))
    assert rows and rows[0] == {"index": 0}
    assert all(row in ({"index": 0}, {"index": 1}) for row in rows)


def test_rotation_unlink_fault_retains_safe_extra_generation(tmp_path, monkeypatch):
    import scripts.okf_loom.studio as module
    path = tmp_path / "events.jsonl"; lock = threading.RLock()
    for index in range(3):
        module._append_jsonl(path, {"index": index}, proc_lock=lock, max_bytes=1, keep=10)
    original = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **kw:
                        (_ for _ in ()).throw(OSError("unlink")) if "events-" in self.name
                        else original(self, *a, **kw))
    module._append_jsonl(path, {"index": 3}, proc_lock=lock, max_bytes=1, keep=1)
    assert [row["index"] for row in module._read_jsonl_with_rotations(path)] == [0, 1, 2, 3]


def test_rotation_generations_never_reused_and_keep_zero_prunes_all(tmp_path):
    import scripts.okf_loom.studio as module
    path = tmp_path / "events.jsonl"; lock = threading.RLock()
    for index in range(8):
        module._append_jsonl(path, {"index": index}, proc_lock=lock, max_bytes=1, keep=2)
    generations = [int(item.stem.rsplit("-", 1)[1]) for item in tmp_path.glob("events-*-*.jsonl")]
    highest = max(generations)
    module._append_jsonl(path, {"index": 8}, proc_lock=lock, max_bytes=1, keep=0)
    assert not list(tmp_path.glob("events-*-*.jsonl"))
    module._append_jsonl(path, {"index": 9}, proc_lock=lock, max_bytes=1, keep=2)
    assert min(int(item.stem.rsplit("-", 1)[1]) for item in tmp_path.glob("events-*-*.jsonl")) > highest


@pytest.mark.parametrize("stem", ["events", "directives"])
def test_rotation_pruning_uses_generation_not_wall_clock_date(tmp_path, stem):
    import scripts.okf_loom.studio as module
    active = tmp_path / f"{stem}.jsonl"
    (tmp_path / f"{stem}-20270102-000000000001.jsonl").write_text('{}\n')
    (tmp_path / f"{stem}-20270102-000000000002.jsonl").write_text('{}\n')
    newest = tmp_path / f"{stem}-20260101-000000000003.jsonl"
    newest.write_text('{}\n')

    module._prune_jsonl_rotations(active, keep=2)

    retained = sorted(tmp_path.glob(f"{stem}-*-*.jsonl"),
                      key=lambda path: module._rotation_sort_key(path, stem, ".jsonl"))
    assert [int(path.stem.rsplit("-", 1)[1]) for path in retained] == [2, 3]
    assert newest in retained


def test_restart_resumes_cursor_and_bundle_rev_from_rotations(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.events_max_bytes = 1; studio.events_keep = 10
    events = [studio.append_event({"type": "proof"}) for _ in range(4)]
    # Last append is active; rotate it explicitly so active history is empty.
    import scripts.okf_loom.studio as module
    with studio._lock: module._rotate_jsonl(studio.events_path, keep=10)
    restarted = Studio.for_bundle(tmp_path)
    assert restarted.seq == events[-1]["event_cursor"]
    assert restarted.bundle_rev == events[-1]["rev"]
    next_event = restarted.append_event({"type": "proof"})
    assert next_event["event_cursor"] == events[-1]["event_cursor"] + 1
    assert next_event["rev"] == events[-1]["rev"] + 1


def test_strict_directive_reader_rejects_complete_or_rotated_corruption(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.session_dir.mkdir(parents=True, exist_ok=True)
    valid = json.dumps({"id": "01JABED9K0", "concept": "note", "state": "open"})
    studio.directives_path.write_text(valid + '\n{bad}\n')
    with pytest.raises(ValueError, match="corrupt JSONL"):
        studio.list_comments()
    studio.directives_path.write_text(valid + '\n{torn')
    assert studio.list_comments()[0]["id"] == "01JABED9K0"
    rotated = studio.session_dir / "directives-20260101-01.jsonl"
    rotated.write_text("{bad}")
    studio.directives_path.write_text("")
    with pytest.raises(ValueError, match="corrupt JSONL"):
        studio.list_comments()


def test_public_directive_list_does_not_translate_capability_failure(tmp_path, monkeypatch):
    import scripts.okf_loom.directive_store as module

    studio = Studio.for_bundle(tmp_path)
    monkeypatch.setattr(module, "_capable", lambda: False)
    with pytest.raises(module.DirectiveStoreError, match="authority is unavailable"):
        studio.list_comments()


def test_strict_wal_schema_rejected_before_recovery(tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    row = studio.post_comment(concept="note", body="x")
    key = "schema-wal-key"
    studio.reanchor_wal_dir.mkdir(parents=True, exist_ok=True)
    wal = studio.reanchor_wal_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    wal.write_text(json.dumps({"state": "prepared", "key": key}))
    with pytest.raises(CommentMutationError, match="unavailable"):
        studio.reanchor_comment(
            comment_id=row["id"], expected_row_token=row["row_token"],
            expected_concept_content_rev=concept_content_rev(source),
            anchor=capture_anchor(source, 0, 6), idempotency_key=key,
            canonical_source=source,
        )


@pytest.mark.parametrize("field,value", [
    ("transaction_id", "unsafe/id"), ("key", "short"), ("fingerprint", "g" * 64),
    ("comment_id", "bad"), ("expected_row_token", ""),
    ("expected_concept_content_rev", "A" * 64), ("created_at", float("inf")),
    ("expires_at", float("nan")), ("state", "unknown"),
])
def test_semantically_malformed_typed_wal_rejected_before_mutation(tmp_path, field, value):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    row = studio.post_comment(concept="note", body="x"); key = "semantic-wal-key"
    kwargs = dict(comment_id=row["id"], expected_row_token=row["row_token"],
                  expected_concept_content_rev=concept_content_rev(source),
                  anchor=capture_anchor(source, 0, 6), idempotency_key=key,
                  canonical_source=source)
    with pytest.raises(OSError): studio.reanchor_comment(**kwargs, fault="after_prepare")
    wal_path = next(path for path in studio.reanchor_wal_dir.glob("*.json")
                    if path not in {studio.reanchor_tombstone_path, studio.reanchor_filter_path})
    wal = json.loads(wal_path.read_text()); wal[field] = value
    wal_path.write_text(json.dumps(wal))
    with pytest.raises(CommentMutationError, match="unavailable"):
        studio.reanchor_comment(**kwargs)
    assert not [item for item in physical_rows(studio) if item.get("reanchor_transaction_id")]


@pytest.mark.parametrize("mode", ["missing", "mismatch"])
def test_finalized_wal_replays_independently_of_physical_directive(mode, tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    row = studio.post_comment(concept="note", body="x"); key = "finalized-row-key"
    kwargs = dict(comment_id=row["id"], expected_row_token=row["row_token"],
                  expected_concept_content_rev=concept_content_rev(source),
                  anchor=capture_anchor(source, 0, 6), idempotency_key=key,
                  canonical_source=source)
    studio.reanchor_comment(**kwargs)
    lines = [json.loads(line) for line in studio.directives_path.read_text().splitlines()]
    if mode == "missing":
        lines = [item for item in lines if not item.get("reanchor_transaction_id")]
    else:
        lines[-1]["reanchor_request_fingerprint"] = "0" * 64
    studio.directives_path.write_text("".join(json.dumps(item) + "\n" for item in lines))
    response, replayed, status = studio.reanchor_comment(**kwargs)
    assert replayed and status == 200 and response["comment"]["id"] == row["id"]


def test_idempotency_reconciliation_rejects_uncertain_tail_and_duplicate_transaction(tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    row = studio.post_comment(concept="note", body="x"); key = "uncertain-tail-key"
    kwargs = dict(comment_id=row["id"], expected_row_token=row["row_token"],
                  expected_concept_content_rev=concept_content_rev(source),
                  anchor=capture_anchor(source, 0, 6), idempotency_key=key,
                  canonical_source=source)
    with pytest.raises(OSError): studio.reanchor_comment(**kwargs, fault="after_prepare")
    with studio.directives_path.open("ab") as fh: fh.write(b'{"torn":')
    with pytest.raises(Exception): studio.reanchor_comment(**kwargs)
    # Repair quarantine for duplicate transaction proof.
    raw = studio.directives_path.read_bytes(); studio.directives_path.write_bytes(raw[:raw.rfind(b'{"torn":')])
    with pytest.raises(OSError): studio.reanchor_comment(**kwargs, fault="after_append")
    lines = studio.directives_path.read_text().splitlines()
    studio.directives_path.write_text("\n".join(lines + [lines[-1]]) + "\n")
    with pytest.raises(CommentMutationError, match="idempotency_conflict"):
        studio.reanchor_comment(**kwargs)


def test_comment_create_wal_cap_tombstone_filter_is_fail_closed(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.COMMENT_CREATE_WAL_CAP = 0
    request = {"concept": "note", "body": "once"}; key = "create-cap-key"
    with pytest.raises(CommentMutationError, match="unavailable"):
        studio.post_comment_idempotent(request=request, idempotency_key=key,
            concept="note", body="once", anchor={}, actor="user", detail={})
    assert studio.list_comments() == []


def test_external_comment_cas_concurrent_claim_edit_one_wins(tmp_path):
    studio = Studio.for_bundle(tmp_path); row = studio.post_comment(concept="note", body="x")
    barrier = threading.Barrier(2); outcomes = []
    def claim():
        barrier.wait(); outcomes.append(studio.update_comment_external(
            row["id"], expected_row_token=row["row_token"], state="claimed")[1])
    def edit():
        barrier.wait(); outcomes.append(studio.update_comment_external(
            row["id"], expected_row_token=row["row_token"], body="edited")[1])
    threads = [threading.Thread(target=claim), threading.Thread(target=edit)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert sorted(str(value) for value in outcomes) == ["None", "stale"]


def test_concurrent_archive_reply_reopen_and_claim_dismiss_preserve_guards(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    root = studio.post_comment(concept="note", body="root", state="resolved")
    reply = studio.post_comment(concept="note", body="reply", parent_id=root["id"], state="resolved")
    barrier = threading.Barrier(2); outcomes = []
    def archive():
        barrier.wait(); outcomes.append(studio.update_comment_external(
            root["id"], expected_row_token=root["row_token"], archived=True)[1])
    def reopen_reply():
        barrier.wait(); studio.update_comment(reply["id"], state="open", claimed_by=None)
    threads = [threading.Thread(target=archive), threading.Thread(target=reopen_reply)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    latest_root, latest_reply = studio.get_comment(root["id"]), studio.get_comment(reply["id"])
    assert not (latest_root.get("archived") and latest_reply.get("state") != "resolved")
    assert outcomes[0] in {None, "archive_unresolved", "stale"}

    opened = studio.post_comment(concept="note", body="race")
    barrier = threading.Barrier(2); results = []
    def claim():
        barrier.wait(); results.append(studio.update_comment_external(
            opened["id"], expected_row_token=opened["row_token"], state="claimed")[1])
    def dismiss():
        barrier.wait(); results.append(studio.update_comment_external(
            opened["id"], expected_row_token=opened["row_token"], state="dismissed")[1])
    threads = [threading.Thread(target=claim), threading.Thread(target=dismiss)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert sorted(str(value) for value in results) == ["None", "stale"]


@pytest.mark.parametrize("filename", ["directives.jsonl", "events.jsonl"])
def test_torn_active_tail_repaired_before_next_append(tmp_path, filename):
    """Crash-mid-append torn suffix is truncated under lock so the next JSONL
    append is a separate valid row; acknowledged complete rows survive; both
    directives and events use the same _append_jsonl helper."""
    import scripts.okf_loom.studio as module
    path = tmp_path / filename; lock = threading.RLock()
    # Seed two complete rows then simulate a crash-mid-append torn suffix.
    path.write_bytes(b'{"index":0}\n{"index":1}\n{"torn":')
    module._append_jsonl(path, {"index": 2}, proc_lock=lock)
    rows = list(module._read_jsonl(path))
    assert [row["index"] for row in rows] == [0, 1, 2]
    # The fresh row is a separate newline-terminated line — no merge.
    lines = path.read_bytes().splitlines(keepends=True)
    assert len(lines) == 3
    assert all(line.endswith(b"\n") for line in lines)
    for line in lines:
        json.loads(line.strip().decode("utf-8"))


def test_torn_tail_entirely_partial_truncates_to_zero(tmp_path):
    """When the entire active file is an unacknowledged partial suffix (no
    complete row), repair truncates to zero and the append starts clean."""
    import scripts.okf_loom.studio as module
    path = tmp_path / "events.jsonl"; lock = threading.RLock()
    path.write_bytes(b'{"totally_torn":')
    module._append_jsonl(path, {"index": 0}, proc_lock=lock)
    assert list(module._read_jsonl(path)) == [{"index": 0}]


def test_torn_tail_with_complete_corruption_remains_fail_closed(tmp_path):
    """Corrupt *complete* newline-terminated rows are never hidden by the
    torn-tail repair: the append fails closed with corrupt JSONL and nothing
    is appended."""
    import scripts.okf_loom.studio as module
    path = tmp_path / "events.jsonl"; lock = threading.RLock()
    path.write_bytes(b'{"ok":true}\n{bad}\n{"torn":')
    with pytest.raises(ValueError, match="corrupt JSONL"):
        module._append_jsonl(path, {"index": 0}, proc_lock=lock)
    # Nothing was appended — file is untouched.
    assert path.read_bytes() == b'{"ok":true}\n{bad}\n{"torn":'


def test_torn_directives_tail_repaired_through_post_comment(tmp_path):
    """End-to-end: a torn directives tail is repaired when the next comment is
    posted, and the persisted directive is a separate valid row."""
    import scripts.okf_loom.studio as module
    studio = Studio.for_bundle(tmp_path); studio.session_dir.mkdir(parents=True, exist_ok=True)
    first = studio.post_comment(concept="note", body="first")
    # Simulate a crash-mid-append torn suffix on the directives file.
    with studio.directives_path.open("ab") as fh:
        fh.write(b'{"torn":')
    second = studio.post_comment(concept="note", body="second")
    rows = list(module._read_jsonl(studio.directives_path))
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    # No merged/phantom line.
    assert len(studio.directives_path.read_bytes().splitlines()) == 2


def test_torn_events_tail_repaired_through_append_event(tmp_path):
    """End-to-end: a torn events tail is repaired when the next event is
    appended, and the persisted event is a separate valid row."""
    import scripts.okf_loom.studio as module
    studio = Studio.for_bundle(tmp_path)
    first = studio.append_event({"type": "proof", "tag": "first"})
    # Simulate a crash-mid-append torn suffix on the events file.
    with studio.events_path.open("ab") as fh:
        fh.write(b'{"torn":')
    second = studio.append_event({"type": "proof", "tag": "second"})
    rows = list(module._read_jsonl(studio.events_path))
    assert [row.get("tag") for row in rows] == ["first", "second"]
    # The fresh event has its own line and a monotonic cursor.
    assert len(studio.events_path.read_bytes().splitlines()) == 2
    assert second["event_cursor"] == first["event_cursor"] + 1


def test_cold_comment_and_unknown_fields_survive_keep_plus_rotations_and_restart(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    studio.DIRECTIVES_MAX_BYTES = 1
    studio.DIRECTIVES_KEEP = 2
    cold = studio.post_comment(concept="note", body="cold", detail={"future": {"v": 1}})
    for index in range(8):
        studio.post_comment(concept="note", body=f"hot-{index}")
    assert len(list(studio.session_dir.glob("directives-*-*.jsonl"))) <= 2
    restarted = Studio.for_bundle(tmp_path)
    row = restarted.get_comment(cold["id"])
    assert row is not None
    assert row["row_token"] == cold["row_token"]
    assert row["detail"] == {"future": {"v": 1}}


def test_checkpoint_failure_retains_generations_and_complete_authority(tmp_path, monkeypatch):
    import scripts.okf_loom.directive_store as directive_module
    studio = Studio.for_bundle(tmp_path)
    studio.DIRECTIVES_MAX_BYTES = 1
    studio.DIRECTIVES_KEEP = 0
    first = studio.post_comment(concept="note", body="first")
    monkeypatch.setattr(directive_module.DirectiveTransaction, "checkpoint",
                        lambda _self: (_ for _ in ()).throw(OSError("checkpoint barrier")))
    with pytest.raises(OSError, match="checkpoint barrier"):
        studio.post_comment(concept="note", body="second")
    # No generation is pruned until a complete atomic checkpoint exists.
    assert list(studio.session_dir.glob("directives-*-*.jsonl"))
    assert Studio.for_bundle(tmp_path).get_comment(first["id"])["body"] == "first"


def test_create_and_reanchor_replay_after_lifecycle_and_event_pruning(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    request = {"concept": "note", "body": "created"}
    created, _ = studio.post_comment_idempotent(
        request=request, idempotency_key="create-lifecycle-key", concept="note",
        body="created", anchor={}, actor="user", detail={"unknown": 1})
    studio.update_comment(created["id"], state="claimed", claimed_by="agent")
    replay, replayed = studio.post_comment_idempotent(
        request=request, idempotency_key="create-lifecycle-key", concept="note",
        body="created", anchor={}, actor="user", detail={"unknown": 1})
    assert replayed and replay["row_token"] == created["row_token"]

    source = "target\n"; current = studio.get_comment(created["id"])
    kwargs = dict(comment_id=created["id"], expected_row_token=current["row_token"],
                  expected_concept_content_rev=concept_content_rev(source),
                  anchor=capture_anchor(source, 0, 6), idempotency_key="reanchor-life-key",
                  canonical_source=source)
    response, _, _ = studio.reanchor_comment(**kwargs)
    studio.update_comment(created["id"], archived=True)
    restarted = Studio.for_bundle(tmp_path)
    restarted.events_max_bytes = 1; restarted.events_keep = 0
    restarted.append_event({"type": "prune"}); before = restarted.seq
    replay_response, replayed, _ = restarted.reanchor_comment(**kwargs)
    assert replayed and replay_response == response
    assert restarted.seq == before  # durable delivered bit prevents a duplicate event


@pytest.mark.parametrize("operation", ["create", "reanchor"])
def test_unexpired_prepared_wal_hard_cap_rejects_new_intent(operation, tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    if operation == "create":
        studio.COMMENT_CREATE_WAL_CAP = 1
        first = {"concept": "note", "body": "first"}
        with pytest.raises(OSError):
            studio.post_comment_idempotent(request=first, idempotency_key="prepared-create-one",
                concept="note", body="first", anchor={}, actor="user", detail={}, fault="after_prepare")
        with pytest.raises(CommentMutationError, match="unavailable"):
            studio.post_comment_idempotent(request={"concept": "note", "body": "second"},
                idempotency_key="prepared-create-two", concept="note", body="second",
                anchor={}, actor="user", detail={})
        paths = [p for p in studio.comment_create_wal_dir.glob("*.json")
                 if p not in {studio.comment_create_tombstone_path, studio.comment_create_filter_path}]
    else:
        studio.REANCHOR_WAL_CAP = 1
        rows = [studio.post_comment(concept="note", body=str(i)) for i in range(2)]
        common = dict(expected_concept_content_rev=concept_content_rev(source),
                      anchor=capture_anchor(source, 0, 6), canonical_source=source)
        with pytest.raises(OSError):
            studio.reanchor_comment(comment_id=rows[0]["id"], expected_row_token=rows[0]["row_token"],
                                    idempotency_key="prepared-reanchor-one", fault="after_prepare", **common)
        with pytest.raises(CommentMutationError, match="unavailable"):
            studio.reanchor_comment(comment_id=rows[1]["id"], expected_row_token=rows[1]["row_token"],
                                    idempotency_key="prepared-reanchor-two", **common)
        paths = [p for p in studio.reanchor_wal_dir.glob("*.json")
                 if p not in {studio.reanchor_tombstone_path, studio.reanchor_filter_path}]
    assert len(paths) == 1


def test_create_prepared_after_append_survives_lifecycle_checkpoint_pruning(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.DIRECTIVES_MAX_BYTES = 1; studio.DIRECTIVES_KEEP = 1
    request = {"concept": "note", "body": "durable", "detail": {"future": 7}}
    kwargs = dict(request=request, idempotency_key="create-pruned-result-key",
                  concept="note", body="durable", anchor={}, actor="user",
                  detail={"future": 7})
    with pytest.raises(OSError, match="after append"):
        studio.post_comment_idempotent(**kwargs, fault="after_append")
    original = next(row for row in studio.list_comments() if row["body"] == "durable")
    # Simulate a crash in the narrow append -> result-index window.  The
    # lifecycle successor must checkpoint the operation result before stripping
    # transaction metadata.
    next(studio.directive_transaction_results_dir.glob("*.json")).unlink()
    studio.update_comment(original["id"], state="claimed", claimed_by="agent")
    for index in range(6): studio.post_comment(concept="note", body=f"rotate-{index}")
    restarted = Studio.for_bundle(tmp_path)
    replay, replayed = restarted.post_comment_idempotent(**kwargs)
    assert replayed and replay["id"] == original["id"]
    assert replay["row_token"] == original["row_token"]
    latest = restarted.get_comment(original["id"])
    assert latest["state"] == "claimed" and latest["detail"] == {"future": 7}
    assert sum(row["id"] == original["id"] for row in restarted.list_comments()) == 1


def test_reanchor_prepared_after_append_survives_lifecycle_checkpoint_pruning(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.DIRECTIVES_MAX_BYTES = 1; studio.DIRECTIVES_KEEP = 1
    source = "target\n"; row = studio.post_comment(
        concept="note", body="x", detail={"future": 9})
    kwargs = dict(comment_id=row["id"], expected_row_token=row["row_token"],
                  expected_concept_content_rev=concept_content_rev(source),
                  anchor=capture_anchor(source, 0, 6),
                  idempotency_key="reanchor-pruned-result-key", canonical_source=source)
    with pytest.raises(OSError, match="after append"):
        studio.reanchor_comment(**kwargs, fault="after_append")
    original_result = next(r for r in physical_rows(studio)
                           if r.get("reanchor_idempotency_key") == kwargs["idempotency_key"])
    next(studio.directive_transaction_results_dir.glob("*.json")).unlink()
    studio.update_comment(row["id"], archived=True)
    for index in range(6): studio.post_comment(concept="note", body=f"rotate-r-{index}")
    response, replayed, status = Studio.for_bundle(tmp_path).reanchor_comment(**kwargs)
    assert replayed and status == 200
    assert response["comment"]["row_token"] == original_result["row_token"]
    latest = Studio.for_bundle(tmp_path).get_comment(row["id"])
    assert latest["archived"] is True and latest["detail"] == {"future": 9}


def test_durable_checkpoint_then_prune_fault_restarts_with_complete_latest_rows(tmp_path, monkeypatch):
    import scripts.okf_loom.directive_store as module
    studio = Studio.for_bundle(tmp_path); studio.DIRECTIVES_MAX_BYTES = 1; studio.DIRECTIVES_KEEP = 0
    first = studio.post_comment(concept="note", body="first", detail={"future": "kept"})
    real_fsync = module._fsync
    monkeypatch.setattr(module, "_fsync",
                        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("after checkpoint")))
    with pytest.raises(OSError, match="after checkpoint"):
        studio.post_comment(concept="note", body="second")
    assert studio.directives_checkpoint_path.is_file()
    monkeypatch.setattr(module, "_fsync", real_fsync)
    restarted = Studio.for_bundle(tmp_path)
    assert restarted.get_comment(first["id"])["detail"] == {"future": "kept"}
    assert {row["body"] for row in restarted.list_comments()} >= {"first", "second"}


def test_prepared_transaction_result_corruption_fails_closed(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    request = {"concept": "note", "body": "once"}
    kwargs = dict(request=request, idempotency_key="corrupt-result-key",
                  concept="note", body="once", anchor={}, actor="user", detail={})
    with pytest.raises(OSError):
        studio.post_comment_idempotent(**kwargs, fault="after_append")
    result_path = next(studio.directive_transaction_results_dir.glob("*.json"))
    result = json.loads(result_path.read_text())
    result["fingerprint"] = "0" * 64
    result_path.write_text(json.dumps(result))
    # Remove the physical transaction proof so reconciliation must validate
    # the durable result index rather than accidentally accepting history.
    studio.directives_path.write_text("")
    with pytest.raises(CommentMutationError, match="idempotency_conflict"):
        Studio.for_bundle(tmp_path).post_comment_idempotent(**kwargs)


@pytest.mark.parametrize("operation", ["create", "reanchor"])
@pytest.mark.parametrize("fault", ["after_notification_pending", "after_notification_append"])
def test_notification_outbox_recovers_both_crash_boundaries(operation, fault, tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    if operation == "create":
        request = {"concept": "note", "body": "notify"}
        kwargs = dict(request=request, idempotency_key="create-notify-crash",
                      concept="note", body="notify", anchor={}, actor="user", detail={})
        with pytest.raises(OSError, match="notification"):
            studio.post_comment_idempotent(**kwargs, fault=fault)
        tx_field = "comment_create_transaction_id"
    else:
        row = studio.post_comment(concept="note", body="notify")
        kwargs = dict(comment_id=row["id"], expected_row_token=row["row_token"],
                      expected_concept_content_rev=concept_content_rev(source),
                      anchor=capture_anchor(source, 0, 6),
                      idempotency_key="reanchor-notify-crash", canonical_source=source)
        with pytest.raises(OSError, match="notification"):
            studio.reanchor_comment(**kwargs, fault=fault)
        tx_field = "reanchor_transaction_id"
    expected_before = 0 if fault == "after_notification_pending" else 1
    assert sum(bool(event.get(tx_field)) for event in studio.read_event_snapshot()) == expected_before
    restarted = Studio.for_bundle(tmp_path)
    if operation == "create": restarted.post_comment_idempotent(**kwargs)
    else: restarted.reanchor_comment(**kwargs)
    assert sum(bool(event.get(tx_field)) for event in restarted.read_event_snapshot()) == 1
    studio.events_max_bytes = 1; studio.events_keep = 0
    studio.append_event({"type": "prune"}); before = studio.seq
    restarted = Studio.for_bundle(tmp_path)
    if operation == "create": restarted.post_comment_idempotent(**kwargs)
    else: restarted.reanchor_comment(**kwargs)
    assert restarted.seq == before


@pytest.mark.parametrize("operation", ["create", "reanchor"])
def test_transaction_result_sidecars_retire_only_after_exact_horizon(operation, tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    if operation == "create":
        request = {"concept": "note", "body": "retire"}
        studio.post_comment_idempotent(request=request, idempotency_key="create-retire-key",
            concept="note", body="retire", anchor={}, actor="user", detail={})
        studio.COMMENT_CREATE_WAL_CAP = 0
        studio._sweep_comment_create_wal_locked(10**12)
        replay, replayed = Studio.for_bundle(tmp_path).post_comment_idempotent(
            request=request, idempotency_key="create-retire-key", concept="note",
            body="retire", anchor={}, actor="user", detail={})
        assert replayed and replay["body"] == "retire"
        studio.COMMENT_CREATE_TOMBSTONE_CAP = 0
        studio.COMMENT_CREATE_TOMBSTONE_TTL_SECONDS = 0
        studio._sweep_comment_create_wal_locked(10**12 + 1)
    else:
        row = studio.post_comment(concept="note", body="retire")
        studio.reanchor_comment(comment_id=row["id"], expected_row_token=row["row_token"],
            expected_concept_content_rev=concept_content_rev(source),
            anchor=capture_anchor(source, 0, 6), idempotency_key="reanchor-retire-key",
            canonical_source=source)
        studio.REANCHOR_WAL_CAP = 0
        studio._sweep_reanchor_wal_locked(now=10**12)
        response, replayed, _ = Studio.for_bundle(tmp_path).reanchor_comment(
            comment_id=row["id"], expected_row_token=row["row_token"],
            expected_concept_content_rev=concept_content_rev(source),
            anchor=capture_anchor(source, 0, 6), idempotency_key="reanchor-retire-key",
            canonical_source=source)
        assert replayed and response["comment"]["id"] == row["id"]
        studio.REANCHOR_TOMBSTONE_CAP = 0; studio.REANCHOR_TOMBSTONE_TTL_SECONDS = 0
        studio._sweep_reanchor_wal_locked(now=10**12 + 1)
    assert not list(studio.directive_transaction_results_dir.glob("*.json"))
    assert not list(studio.notification_receipts_dir.glob("*.json"))


def test_keyless_notification_dedup_compacts_to_bounded_authority(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    row = studio.post_comment(concept="note", body="keyless")
    txid = row["notification_transaction_id"]
    assert sum(event.get("comment_create_transaction_id") == txid
               for event in studio.read_event_snapshot()) == 1
    studio._cleanup_transaction_sidecars_locked()
    assert not (studio.notification_receipts_dir / f"{txid}.json").exists()
    assert studio._keyless_notification_filter_contains(txid)
    studio.events_max_bytes = 1; studio.events_keep = 0
    studio.append_event({"type": "prune"}); before = studio.seq
    restarted = Studio.for_bundle(tmp_path)
    assert restarted.seq == before
    assert restarted._keyless_notification_filter_contains(txid)


def test_claim_expiry_uses_claim_transition_time_not_creation(tmp_path, monkeypatch):
    import scripts.okf_loom.studio as module
    studio = Studio.for_bundle(tmp_path); studio.claim_ttl_s = 60
    row = studio.post_comment(concept="note", body="old")
    old = dict(row, ts="2000-01-01T00:00:00Z")
    studio.directives_path.write_text(json.dumps(old) + "\n")
    claimed = studio.update_comment(row["id"], state="claimed", claimed_by="agent")
    assert studio.sweep_stale_presence_and_claims()["claims_reverted"] == []
    stale = dict(claimed, claimed_at="2000-01-01T00:00:00Z",
                 updated_at="2000-01-01T00:00:00Z", row_token="r2:01KX9ZZZZZ0000000000000000")
    with module.DirectiveStore(studio.session_dir).writer() as transaction:
        studio._append_directive_locked(transaction, stale)
    assert studio.sweep_stale_presence_and_claims()["claims_reverted"] == [row["id"]]


def test_claim_timestamp_is_owned_preserved_changed_and_cleared(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    row = studio.post_comment(concept="note", body="claim")
    claimed = studio.update_comment(row["id"], state="claimed", claimed_by="agent-a")
    assert claimed["claimed_at"]
    edited = studio.update_comment(row["id"], body="unrelated edit")
    assert edited["claimed_at"] == claimed["claimed_at"]
    changed = studio.update_comment(row["id"], claimed_by="agent-b")
    assert changed["claimed_at"] >= claimed["claimed_at"]
    opened = studio.update_comment(row["id"], state="open", claimed_by=None)
    assert "claimed_at" not in opened


def test_malformed_claim_timestamp_is_visible_as_quarantined(tmp_path):
    import scripts.okf_loom.directive_store as module
    studio = Studio.for_bundle(tmp_path)
    row = studio.post_comment(concept="note", body="claim")
    claimed = studio.update_comment(row["id"], state="claimed", claimed_by="agent")
    with module.DirectiveStore(studio.session_dir).writer() as transaction:
        studio._append_directive_locked(transaction, dict(
            claimed, claimed_at="bad-time",
            row_token="r2:01KX9ZZZZZ0000000000000000"))
    result = studio.sweep_stale_presence_and_claims()
    assert result["claims_quarantined"] == [row["id"]]


def test_notification_receipt_symlink_is_rejected(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    studio.notification_receipts_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.json"; target.write_text("{}")
    (studio.notification_receipts_dir / "01KX9ZZZZZ0000000000000000.json").symlink_to(target)
    with pytest.raises(CommentMutationError, match="unavailable"):
        studio._recover_comment_notifications()


def test_keyless_notification_authority_obeys_low_cap_across_restart(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.KEYLESS_NOTIFICATION_FILTER_CAP = 2
    txids = []
    for index in range(3):
        row = studio.post_comment(concept="note", body=str(index))
        txids.append(row["notification_transaction_id"])
    studio._cleanup_transaction_sidecars_locked()
    retained = studio._keyless_notification_filter()
    assert len(retained) == 2 and txids[-1] in retained
    restarted = Studio.for_bundle(tmp_path); restarted.KEYLESS_NOTIFICATION_FILTER_CAP = 2
    assert restarted._keyless_notification_filter() == retained


def test_notification_checkpoint_cap_eviction_prune_restart_has_no_duplicate(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.KEYLESS_NOTIFICATION_FILTER_CAP = 1
    rows = [studio.post_comment(concept="note", body=str(index)) for index in range(3)]
    evicted_txid = rows[0]["notification_transaction_id"]
    studio._cleanup_transaction_sidecars_locked()
    assert evicted_txid not in studio._keyless_notification_filter()
    assert studio._keyless_notification_filter_contains(evicted_txid)
    studio.events_max_bytes = 1; studio.events_keep = 0
    studio.append_event({"type": "prune"}); before = studio.seq
    restarted = Studio.for_bundle(tmp_path); restarted.KEYLESS_NOTIFICATION_FILTER_CAP = 1
    assert restarted.seq == before
    assert restarted._keyless_notification_filter_contains(evicted_txid)
    restarted._recover_comment_notifications()
    assert sum(event.get("comment_create_transaction_id") == evicted_txid
               for event in restarted.read_event_snapshot()) == 0


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_notification_checkpoint_fails_closed(version, tmp_path):
    studio = Studio.for_bundle(tmp_path)
    studio.session_dir.mkdir(parents=True, exist_ok=True)
    studio.keyless_notification_filter_path.write_text(json.dumps({
        "version": version, "transactions": ["01KX9ZZZZZ0000000000000000"]
    }))
    with pytest.raises(CommentMutationError, match="unavailable"):
        studio._keyless_notification_filter_state()


def test_crash_receipt_temporary_is_removed_without_bricking_recovery(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    studio.notification_receipts_dir.mkdir(parents=True, exist_ok=True)
    temporary = studio.notification_receipts_dir / \
        ".01KX9ZZZZZ0000000000000000.json.0123456789abcdef.tmp"
    temporary.write_text("partial")
    studio._recover_comment_notifications()
    assert not temporary.exists()


def test_forced_rotation_checkpoint_keeps_recovery_receipt_directory_pinned(
        tmp_path, monkeypatch):
    import scripts.okf_loom.studio as studio_module

    studio = Studio.for_bundle(tmp_path)
    studio.append_event({"type": "seed-rotation"})
    studio.events_max_bytes = 1
    request = {"concept": "note", "body": "trusted"}
    kwargs = dict(request=request, idempotency_key="pinned-recovery-directory",
                  concept="note", body="trusted", anchor={}, actor="user", detail={})
    with pytest.raises(OSError, match="notification pending"):
        studio.post_comment_idempotent(**kwargs, fault="after_notification_pending")
    receipt_path = next(studio.notification_receipts_dir.glob("*.json"))
    replacement = json.loads(receipt_path.read_text())
    replacement["event"]["body"] = "redirected"

    original_listdir = studio_module.os.listdir
    substituted = False

    def substitute_after_enumeration(directory_fd):
        nonlocal substituted
        names = original_listdir(directory_fd)
        if not substituted:
            substituted = True
            receipts = studio.session_dir / ".notification-receipts"
            pinned_path = receipts.with_name(".receipts-pinned")
            receipts.rename(pinned_path)
            receipts.mkdir(mode=0o700)
            (receipts / receipt_path.name).write_text(
                json.dumps(replacement), encoding="utf-8")
        return names

    monkeypatch.setattr(studio_module.os, "listdir", substitute_after_enumeration)
    studio._recover_comment_notifications()

    delivered = [event for event in studio.read_event_snapshot()
                 if event.get("comment_create_transaction_id") == receipt_path.stem]
    assert len(delivered) == 1
    assert delivered[0]["body"] == "trusted"
    assert json.loads((studio.notification_receipts_dir / receipt_path.name).read_text())[
        "state"] == "committed"
    assert json.loads((studio.session_dir / ".notification-receipts" /
                       receipt_path.name).read_text())["state"] == "pending"
    assert list(studio.session_dir.glob("events-*-*.jsonl"))


def test_missing_receipt_child_is_created_from_pinned_session_parent(
        tmp_path, monkeypatch):
    studio = Studio.for_bundle(tmp_path)
    row = studio.post_comment(concept="note", body="missing child")
    receipts = studio.session_dir / ".notification-receipts"
    for child in receipts.iterdir():
        child.unlink()
    studio._session_descendants.pop(".notification-receipts").close()
    receipts.rmdir()
    assert not receipts.exists()
    directives = studio.list_comments()
    original_session = studio.session_dir.with_name("session-pinned")
    substituted = False

    def substitute_session_before_reconciliation():
        nonlocal substituted
        if not substituted:
            substituted = True
            studio.session_dir.rename(original_session)
            studio.session_dir.mkdir(mode=0o700)
        return directives

    monkeypatch.setattr(studio, "list_comments", substitute_session_before_reconciliation)
    studio._recover_comment_notifications()

    # The missing child remains absent; recovery does not consult or create in
    # the replacement namespace after the pinned session parent is renamed.
    assert not (studio.session_dir / ".notification-receipts").exists()


def test_receipt_cleanup_cannot_unlink_from_substituted_directory(tmp_path, monkeypatch):
    import scripts.okf_loom.studio as studio_module

    studio = Studio.for_bundle(tmp_path)
    row = studio.post_comment(concept="note", body="cleanup pinned")
    studio._commit_notification_identity(
        row["notification_transaction_id"], "create",
        studio._comment_event(row, transaction_id=row["notification_transaction_id"]))
    receipt_path = studio.notification_receipts_dir / \
        f"{row['notification_transaction_id']}.json"
    replacement = receipt_path.read_text(encoding="utf-8")
    original_listdir = studio_module.os.listdir
    substituted = False

    def substitute_after_enumeration(directory_fd):
        nonlocal substituted
        names = original_listdir(directory_fd)
        if not substituted:
            substituted = True
            receipts = studio.session_dir / ".notification-receipts"
            pinned_path = receipts.with_name(".cleanup-pinned")
            receipts.rename(pinned_path)
            receipts.mkdir(mode=0o700)
            (receipts / receipt_path.name).write_text(
                replacement, encoding="utf-8")
        return names

    monkeypatch.setattr(studio_module.os, "listdir", substitute_after_enumeration)
    studio._cleanup_transaction_sidecars_locked()

    assert not (studio.session_dir / ".cleanup-pinned" / receipt_path.name).exists()
    assert (studio.session_dir / ".notification-receipts" /
            receipt_path.name).exists()


def test_notification_receipt_directory_symlink_is_rejected_without_escape(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    outside = tmp_path / "outside"; outside.mkdir()
    studio.session_dir.mkdir(parents=True, exist_ok=True)
    (studio.session_dir / ".notification-receipts").symlink_to(
        outside, target_is_directory=True)
    with pytest.raises(OSError):
        studio.post_comment(concept="note", body="blocked")
    assert list(outside.iterdir()) == []


def test_legacy_unique_resolution_returns_current_public_block_shape():
    source = "# Heading\n\nlegacy target text\n"
    result = resolve_anchor({"kind": "text", "ref": "target"}, source)
    assert result["status"] == "resolved"
    assert result["position"] == {"start": source.index("target"), "end": source.index("target") + 6}
    assert set(result["block"]) == {"id", "start", "end", "hash"}


@pytest.mark.parametrize("operation", ["create", "reanchor"])
def test_exact_tombstone_protects_transaction_result_until_filter_retirement(operation, tmp_path):
    studio = Studio.for_bundle(tmp_path); source = "target\n"
    if operation == "create":
        request = {"concept": "note", "body": "horizon"}
        studio.post_comment_idempotent(request=request, idempotency_key="create-horizon-key",
            concept="note", body="horizon", anchor={}, actor="user", detail={})
        studio.COMMENT_CREATE_WAL_CAP = 0
        studio._sweep_comment_create_wal_locked(10**12)
        tombstone_path = studio.comment_create_tombstone_path
        studio.COMMENT_CREATE_TOMBSTONE_CAP = 0; studio.COMMENT_CREATE_TOMBSTONE_TTL_SECONDS = 0
        sweep = lambda: studio._sweep_comment_create_wal_locked(10**12 + 1)
    else:
        row = studio.post_comment(concept="note", body="horizon")
        studio.reanchor_comment(comment_id=row["id"], expected_row_token=row["row_token"],
            expected_concept_content_rev=concept_content_rev(source),
            anchor=capture_anchor(source, 0, 6), idempotency_key="reanchor-horizon-key",
            canonical_source=source)
        studio.REANCHOR_WAL_CAP = 0
        studio._sweep_reanchor_wal_locked(now=10**12)
        tombstone_path = studio.reanchor_tombstone_path
        studio.REANCHOR_TOMBSTONE_CAP = 0; studio.REANCHOR_TOMBSTONE_TTL_SECONDS = 0
        sweep = lambda: studio._sweep_reanchor_wal_locked(now=10**12 + 1)
    results = list(studio.directive_transaction_results_dir.glob("*.json"))
    assert len(results) == 1
    original = tombstone_path.read_text()
    tombstone_path.write_text("{corrupt")
    with pytest.raises(CommentMutationError, match="unavailable"):
        studio._cleanup_transaction_sidecars_locked()
    assert results[0].exists()
    tombstone_path.write_text(original)
    sweep()
    assert not results[0].exists()


def test_pending_create_receipt_is_full_immutable_authority_across_lifecycle(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    request = {"concept": "note", "body": "original"}
    kwargs = dict(request=request, idempotency_key="immutable-create-event",
                  concept="note", body="original", anchor={}, actor="user", detail={})
    with pytest.raises(OSError, match="notification pending"):
        studio.post_comment_idempotent(**kwargs, fault="after_notification_pending")
    receipt_path = next(studio.notification_receipts_dir.glob("*.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["state"] == "pending"
    assert receipt["event"]["body"] == "original"
    assert receipt["event"]["state"] == "open"
    row = studio.get_comment(receipt["event"]["id"])
    studio.update_comment(row["id"], state="claimed", claimed_by="agent", body="changed")
    Studio.for_bundle(tmp_path)._recover_comment_notifications()
    delivered = [event for event in studio.read_event_snapshot()
                 if event.get("comment_create_transaction_id") == receipt["transaction_id"]]
    assert len(delivered) == 1
    assert delivered[0]["body"] == "original" and delivered[0]["state"] == "open"


def test_pending_notification_payload_tamper_fails_closed_before_append(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    request = {"concept": "note", "body": "original"}
    kwargs = dict(request=request, idempotency_key="tampered-create-event",
                  concept="note", body="original", anchor={}, actor="user", detail={})
    with pytest.raises(OSError):
        studio.post_comment_idempotent(**kwargs, fault="after_notification_pending")
    receipt_path = next(studio.notification_receipts_dir.glob("*.json"))
    receipt = json.loads(receipt_path.read_text()); receipt["event"].pop("body")
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(CommentMutationError, match="unavailable"):
        Studio.for_bundle(tmp_path).post_comment_idempotent(**kwargs)
    assert not [event for event in studio.read_event_snapshot()
                if event.get("comment_create_transaction_id") == receipt["transaction_id"]]


def test_create_receipt_is_durable_before_directive_visibility_and_retry_owns_it(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    request = {"concept": "note", "body": "crash boundary"}
    kwargs = dict(request=request, idempotency_key="receipt-before-directive",
                  concept="note", body="crash boundary", anchor={}, actor="user", detail={})
    with pytest.raises(OSError, match="before directive"):
        studio.post_comment_idempotent(**kwargs, fault="after_notification_reservation")
    assert studio.list_comments() == []
    receipt_path = next(studio.notification_receipts_dir.glob("*.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["state"] == "pending" and receipt["event"]["body"] == "crash boundary"
    row, replayed = Studio.for_bundle(tmp_path).post_comment_idempotent(**kwargs)
    assert replayed is True
    assert row["id"] == receipt["event"]["id"]
    events = [event for event in studio.read_event_snapshot()
              if event.get("comment_create_transaction_id") == receipt["transaction_id"]]
    assert len(events) == 1 and events[0]["body"] == "crash boundary"


@pytest.mark.parametrize("field,value", [
    ("type", "comments"), ("id", "bad/id"), ("state", "claimed"),
    ("actor", "root"), ("actor", None), ("claimed_by", 7),
    ("claimed_by", "agent"), ("row_token", "bad token"),
    ("ts", "yesterday"), ("updated_at", 7), ("reply", []),
    ("summary", {}), ("request_summary", []), ("resolved_activity", [7]),
    ("anchor", []), ("body", 7), ("concept", 7), ("archived", 1),
    ("comment_create_transaction_id", "wrong"), ("parent_id", "bad/id"),
])
def test_pending_notification_every_canonical_field_tamper_fails_closed(
        tmp_path, field, value):
    studio = Studio.for_bundle(tmp_path)
    request = {"concept": "note", "body": "original"}
    kwargs = dict(request=request, idempotency_key="tamper-field-" + field,
                  concept="note", body="original", anchor={}, actor="user", detail={})
    with pytest.raises(OSError):
        studio.post_comment_idempotent(**kwargs, fault="after_notification_pending")
    receipt_path = next(studio.notification_receipts_dir.glob("*.json"))
    receipt = json.loads(receipt_path.read_text()); receipt["event"][field] = value
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(CommentMutationError, match="unavailable"):
        Studio.for_bundle(tmp_path)._recover_comment_notifications()


@pytest.mark.parametrize("mutation", [
    {"state": "claimed", "claimed_by": "agent"},
    {"body": "edited"}, {"state": "resolved"}, {"archived": True},
])
def test_lifecycle_notifications_use_full_canonical_payload_with_new_token(tmp_path, mutation):
    studio = Studio.for_bundle(tmp_path)
    row = studio.post_comment(concept="note", body="original")
    updated = studio.update_comment(row["id"], **mutation)
    event = studio.read_event_snapshot()[-1]
    transport = {"event_id", "event_cursor", "seq", "rev"}
    # The lifecycle event names an actor only when the mutation itself
    # carries that evidence: an explicit claim/resolve with claimed_by.
    # Every other operation stays actorless — an inherited row claim is not
    # proof of who edited, resolved, or archived.
    evidenced_actor = (
        mutation["claimed_by"]
        if mutation.get("state") in {"claimed", "resolved"} and "claimed_by" in mutation
        else None
    )
    assert {key: value for key, value in event.items() if key not in transport} == studio._comment_event(
        updated, operation="lifecycle", mutation_actor=evidenced_actor)
    assert event["row_token"] == updated["row_token"] != row["row_token"]


def test_external_lifecycle_allows_dismissed_reopen_but_not_claimed_or_resolved(tmp_path):
    studio = Studio.for_bundle(tmp_path)
    opened = studio.post_comment(concept="note", body="open")
    dismissed, error, _ = studio.update_comment_external(
        opened["id"], expected_row_token=opened["row_token"], state="dismissed")
    assert error is None
    reopened, error, _ = studio.update_comment_external(
        opened["id"], expected_row_token=dismissed["row_token"], state="open")
    assert error is None and reopened["claimed_by"] is None
    for state in ("claimed", "resolved"):
        row = studio.post_comment(concept="note", body=state,
                                  state="resolved" if state == "resolved" else "open")
        if state == "claimed":
            row = studio.update_comment(row["id"], state="claimed", claimed_by="agent")
        unchanged = dict(studio.get_comment(row["id"]))
        result, error, _ = studio.update_comment_external(
            row["id"], expected_row_token=row["row_token"], state="open")
        assert result is None and error == "invalid_transition"
        assert studio.get_comment(row["id"]) == unchanged
