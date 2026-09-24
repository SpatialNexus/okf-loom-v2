"""Bundle 3 endpoint prepare/replay authority proofs."""
from __future__ import annotations

import os
import multiprocessing

import pytest

from okf_loom.proposal_review.errors import IdempotencyConflictError
from okf_loom.proposal_review import canonical
from okf_loom.proposal_review.ports import LinuxDirFdFilesystem
from okf_loom.proposal_review.repository import ProposalRepositoryV1
from test_proposal_apply import _ready_transaction
from test_proposal_crash import (_child_apply_key, DiskActivity, DiskEvents, _open,
                                 _killed, _recover_existing)

TS = "2026-07-14T12:34:56.789Z"
EXP = "2026-07-21T12:34:56.789Z"
D0 = "sha256:" + "0" * 64
D1 = "sha256:" + "1" * 64


class Clock:
    def now(self): return TS


class Entropy:
    def __init__(self): self.n = 0
    def token_bytes(self, count):
        self.n += 1
        return self.n.to_bytes(count, "big")


def repo(tmp_path):
    root = tmp_path / "session"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    return fs, ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())


def append_orphan_prepare(repository):
    body = {"schema": 1, "record_id": "rec_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
            "scope": "/__proposal/v1/proposals", "key_digest": D0,
            "request_fingerprint": D0, "state": "prepared", "http_status": None,
            "response_digest": None, "response": None, "transaction_id": None,
            "created_at": TS, "expires_at": EXP}
    body["checksum"] = canonical.compute_checksum(body)
    repository.append("endpoint_results", "endpoint_result", body)


def test_prepare_is_durable_and_same_key_changed_fingerprint_conflicts(tmp_path) -> None:
    fs, first = repo(tmp_path)
    scope = "/__proposal/v1/proposals"
    append_orphan_prepare(first)
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    replay = reopened.reconcile_endpoint(scope, D0, D0)
    assert replay.state == "terminal_result"
    assert replay.endpoint_result.state == "indeterminate"
    with pytest.raises(IdempotencyConflictError):
        reopened.reconcile_endpoint(scope, D0, D1)


def test_prepared_create_without_link_is_not_clean_new_authority(tmp_path) -> None:
    fs, repository = repo(tmp_path)
    scope = "/__proposal/v1/proposals"
    append_orphan_prepare(repository)
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    result = reopened.reconcile_endpoint(scope, D0, D0)
    assert result.state == "terminal_result"
    assert result.endpoint_result.state == "indeterminate"


def test_state_10_result_tombstone(tmp_path) -> None:
    root = tmp_path / "retention"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    class MutableClock:
        value = TS
        def now(self): return self.value
    clock = MutableClock(); repository = ProposalRepositoryV1.bootstrap(fs, clock=clock, entropy=Entropy())
    response = {"schema": 1, "data": {"ok": True}}
    body = {"schema": 1, "record_id": "rec_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
            "scope": "/__proposal/v1/proposals", "key_digest": D0,
            "request_fingerprint": D0, "state": "terminal", "http_status": 201,
            "response_digest": canonical.canonical_digest(response), "response": response,
            "transaction_id": None, "created_at": TS, "expires_at": EXP}
    body["checksum"] = canonical.compute_checksum(body)
    repository.append("endpoint_results", "endpoint_result", body)
    clock.value = "2026-07-21T12:34:56.789Z"
    result = repository.reconcile_endpoint(body["scope"], D0, D0)
    assert result.state == "terminal_compacted"
    assert result.tombstone.result_record_id == body["record_id"]
    assert result.tombstone.expires_at == "2027-01-10T12:34:56.789Z"


def test_idem_01_apply_race(tmp_path) -> None:
    bundle, session, _, _, publication, request, _ = _ready_transaction(tmp_path)
    publication.close()
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_child_apply_key,
        args=(str(bundle), str(session), request, "never", "K" * 32)) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(30)
    assert [worker.exitcode for worker in workers] == [0, 0]
    repository = _open(session, DiskActivity(session))
    audits = [row.payload for row in repository.snapshot().aggregate("transactions")
              if row.kind == "transaction" and row.payload.get("operation") == "apply"]
    assert len(audits) == 1


def test_idem_07_different_keys(tmp_path) -> None:
    bundle, session, _, _, publication, request, _ = _ready_transaction(tmp_path)
    publication.close(); context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_child_apply_key,
        args=(str(bundle), str(session), request, "never", char * 32)) for char in ("K", "L")]
    for worker in workers: worker.start()
    for worker in workers: worker.join(30)
    assert sorted(worker.exitcode for worker in workers) == [0, 78]
    repository = _open(session, DiskActivity(session))
    audits = [row.payload for row in repository.snapshot().aggregate("transactions")
              if row.kind == "transaction" and row.payload.get("operation") == "apply"]
    assert len(audits) == 1


def test_idem_03_fingerprint(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, _ = _ready_transaction(tmp_path)
    try:
        coordinator.apply(request, "K" * 32)
        changed = dict(request); changed["actor"] = "different"
        with pytest.raises(IdempotencyConflictError): coordinator.apply(changed, "K" * 32)
    finally: publication.close()


def test_idem_04_lost_response(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, _ = _ready_transaction(tmp_path)
    try:
        first = coordinator.apply(request, "K" * 32)
        replay = coordinator.apply(request, "K" * 32)
        assert replay.transaction_id == first.transaction_id and replay.replayed
        assert replay.concept_revisions == first.concept_revisions
    finally: publication.close()


def test_idem_02_apply_crashes(tmp_path) -> None:
    for index, boundary in enumerate(("wal_prepared", "commit_decided",
                                      "publish_bitmap_advanced", "audit_appended",
                                      "endpoint_terminal", "receipt_updated")):
        root = tmp_path / str(index); root.mkdir()
        bundle, session, repository, coordinator, publication, request = _killed(root, boundary)
        try:
            try: coordinator.recover_all()
            except Exception: pass
            if boundary == "wal_prepared":
                with pytest.raises(Exception) as rejected: coordinator.apply(request, "K" * 32)
                assert getattr(rejected.value, "code", None) == "apply_rejected"
                first_wire = rejected.value.persisted_error.to_mapping()
                with pytest.raises(Exception) as replayed:
                    coordinator.apply(request, "K" * 32)
                assert replayed.value.persisted_error.to_mapping() == first_wire
                assert replayed.value.persisted_error.status == 409
            else:
                result = coordinator.apply(request, "K" * 32)
                assert result.status == "applied" and result.replayed
            audits = [row for row in repository.snapshot().aggregate("transactions")
                      if row.kind == "transaction" and row.payload.get("operation") == "apply"]
            assert len(audits) == 1
        finally: publication.close()


def test_idem_05_compaction(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, _ = _ready_transaction(tmp_path)
    try:
        first = coordinator.apply(request, "K" * 32)
        before = (bundle / "tables/orders.md").stat().st_ino
        class ExpiredClock:
            def now(self): return "2026-07-22T12:34:56.789Z"
        repository._clock = ExpiredClock()
        with pytest.raises(Exception) as compacted:
            coordinator.apply(request, "K" * 32)
        assert getattr(compacted.value, "code", None) == "indeterminate_outcome"
        tombstones = [row.payload for row in repository.snapshot().aggregate("tombstones")
                      if row.kind == "tombstone" and row.payload.get("transaction_id") == first.transaction_id]
        assert len(tombstones) == 1 and tombstones[0]["outcome"] == "terminal_compacted"
        assert (bundle / "tables/orders.md").stat().st_ino == before
    finally: publication.close()


def test_idem_06_uncertain(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    publication.close(); transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    stage = next((session / f"proposal-review-v1/transactions/{transaction_id}/stage").iterdir())
    stage.unlink(); stage.write_bytes(b"substituted"); stage.chmod(0o600)
    _, reopened = _recover_existing(bundle, session, indeterminate=True)
    assert reopened.read_wal(transaction_id).phase == "indeterminate"


def test_idem_08_outbox(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, _ = _ready_transaction(tmp_path)
    class Failing:
        def append_proposal_event(self, outbox_id, payload): raise OSError("event store unavailable")
    coordinator.outbox = Failing()
    try:
        first = coordinator.apply(request, "K" * 32)
        assert repository.pending_outbox()
        class Recording:
            def __init__(self): self.ids = []
            def append_proposal_event(self, outbox_id, payload):
                self.ids.append(outbox_id); return len(self.ids)
        delivered = Recording(); coordinator.outbox = delivered
        coordinator.recover_all()
        assert len(delivered.ids) == 1
        assert not [row for row in repository.pending_outbox()
                    if row.aggregate_kind == "transaction"]
        replay = coordinator.apply(request, "K" * 32)
        assert replay.transaction_id == first.transaction_id and replay.replayed
        assert len([row for row in repository.snapshot().aggregate("transactions")
                    if row.kind == "transaction" and row.payload.get("operation") == "apply"]) == 1
    finally: publication.close()
