import time

import pytest

from okf_loom.proposal_review import schemas
from test_proposal_apply import _ready_transaction


def _wait_terminal(repository, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        rows = [row.payload for row in repository.snapshot().aggregate("endpoint_results")
                if row.kind == "endpoint_result" and row.payload.get("state") == "terminal"
                and row.payload.get("transaction_id") is not None]
        if rows: return rows[-1]
        time.sleep(.01)
    raise AssertionError("late transaction did not become terminal")


def test_async_07_apply_before_commit_decision(tmp_path):
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    delayed = False
    def fault(boundary, evidence):
        nonlocal delayed
        if boundary == "wal_prepared" and not delayed:
            delayed = True; time.sleep(1.2)
    coordinator.fault_injector = fault; coordinator.wait_seconds = .8
    try:
        progress = coordinator.apply(request, "K" * 32)
        assert isinstance(progress, schemas.TransactionProgress)
        assert progress.phase == "prepared" and not progress.commit_decided
        _wait_terminal(repository)
        replay = coordinator.apply(request, "K" * 32)
        assert replay.status == "applied" and replay.replayed
        assert not coordinator._waiters
    finally: publication.close()


def test_async_08_apply_after_commit_decision(tmp_path):
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    delayed = False
    def fault(boundary, evidence):
        nonlocal delayed
        if boundary == "commit_decided" and not delayed:
            delayed = True; time.sleep(1.2)
    coordinator.fault_injector = fault; coordinator.wait_seconds = .8
    try:
        progress = coordinator.apply(request, "K" * 32)
        assert isinstance(progress, schemas.TransactionProgress)
        assert progress.commit_decided
        terminal = _wait_terminal(repository)
        assert terminal["http_status"] == 200
        replay = coordinator.apply(request, "K" * 32)
        assert replay.status == "applied" and replay.replayed
        assert not coordinator._waiters
    finally: publication.close()


def test_async_late_error_persists_rollback_and_cleans_waiter(tmp_path):
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    def fault(boundary, evidence):
        if boundary == "wal_prepared":
            time.sleep(1.2)
            raise RuntimeError("injected pre-decision worker failure")
    coordinator.fault_injector = fault; coordinator.wait_seconds = .8
    try:
        progress = coordinator.apply(request, "K" * 32)
        assert progress.phase == "prepared"
        time.sleep(.6)
        with pytest.raises(Exception) as rejected:
            coordinator.apply(request, "K" * 32)
        assert getattr(rejected.value, "code", None) == "apply_rejected", repr(rejected.value)
        assert not coordinator._waiters
        assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()
    finally: publication.close()


def _transaction_authority(repository, session):
    intents = list((session / "proposal-review-v1/transactions").glob("*/intent.json"))
    wals = list((session / "proposal-review-v1/transactions").glob("*/wal.json"))
    endpoints = [row.payload for row in repository.snapshot().aggregate("endpoint_results")
                 if row.kind == "endpoint_result" and row.payload.get("transaction_id")]
    audits = [row for row in repository.snapshot().aggregate("transactions")
              if row.kind == "transaction"]
    return intents, wals, endpoints, audits


def test_pre_authority_timeout_fence_cancels_without_late_authority(tmp_path):
    import threading
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    at_fence = threading.Barrier(2); release = threading.Event()
    def pause(boundary, evidence):
        if boundary == "before_intent_admission":
            at_fence.wait(timeout=5); release.wait(10)
    coordinator.fault_injector = pause; coordinator.wait_seconds = .2
    outcome = {}
    caller = threading.Thread(target=lambda: _capture_call(
        outcome, lambda: coordinator.apply(request, "K" * 32)))
    try:
        caller.start(); at_fence.wait(timeout=5); caller.join(5)
        assert not caller.is_alive()
        assert getattr(outcome["error"], "code", None) == "operation_timeout"
        assert _transaction_authority(repository, session) == ([], [], [], [])
        release.set()
        with coordinator._workers_lock:
            workers = tuple(coordinator._workers)
        for worker in workers: worker.join(5)
        assert not [worker for worker in workers if worker.is_alive()]
        assert _transaction_authority(repository, session) == ([], [], [], [])
        assert not list((session / "proposal-review-v1/transactions").iterdir())
        assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()
    finally:
        release.set(); publication.close()


def _capture_call(outcome, call):
    try: outcome["result"] = call()
    except BaseException as exc: outcome["error"] = exc


@pytest.mark.parametrize("defer_admission", [False, True], ids=["ready", "delayed"])
def test_pre_authority_timeout_admission_wins_same_key_retry_has_one_transaction(tmp_path, monkeypatch, defer_admission):
    import threading
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    admitted = threading.Barrier(2); release = threading.Event()
    intent_published = threading.Event(); cancellation_started = threading.Event()
    cancel_admission = repository.cancel_transaction_admission
    def cancel_after_intent(admission):
        # Force this proof's admission-wins ordering at the real fence. Slow
        # pre-admission I/O can otherwise make the unchanged 200ms wait expire
        # before the intent exists, legitimately exercising the opposite case.
        cancellation_started.set()
        assert intent_published.wait(5), "worker did not publish its intent"
        return cancel_admission(admission)
    monkeypatch.setattr(repository, "cancel_transaction_admission", cancel_after_intent)
    def pause(boundary, evidence):
        if boundary == "before_intent_admission" and defer_admission:
            assert cancellation_started.wait(5), "caller did not reach cancellation fence"
        if boundary == "intent_published":
            intent_published.set()
            admitted.wait(timeout=5); release.wait(10)
    coordinator.fault_injector = pause; coordinator.wait_seconds = .2
    first, second = {}, {}
    caller = threading.Thread(target=lambda: _capture_call(
        first, lambda: coordinator.apply(request, "K" * 32)))
    retry = threading.Thread(target=lambda: _capture_call(
        second, lambda: coordinator.apply(request, "K" * 32)))
    try:
        caller.start(); admitted.wait(timeout=5); caller.join(5)
        assert not caller.is_alive()
        assert getattr(first["error"], "code", None) == "transaction_recovering"
        intents, wals, endpoints, audits = _transaction_authority(repository, session)
        assert len(intents) == 1 and not wals and not endpoints and not audits
        coordinator.wait_seconds = 5
        retry.start(); release.set(); retry.join(20)
        assert not retry.is_alive() and "error" not in second
        result = second["result"]
        assert result.status == "applied"
        intents, wals, endpoints, audits = _transaction_authority(repository, session)
        assert len(intents) == len(wals) == len(audits) == 1
        assert len([row for row in endpoints if row["state"] == "terminal"]) == 1
        assert {row["transaction_id"] for row in endpoints} == {result.transaction_id}
        assert b"tags:" in (bundle / "tables/orders.md").read_bytes()
    finally:
        release.set(); publication.close()
