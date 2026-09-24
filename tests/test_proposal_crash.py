"""Real process-death/reopen proofs for the transaction WAL boundaries."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from okf_loom.proposal_review.ports import LinuxDirFdFilesystem, SystemEntropy
from okf_loom.proposal_review.repository import ProposalRepositoryV1
from okf_loom.proposal_review.publication import TrustedPublicationAdapter
from okf_loom.proposal_review.transactions import ProposalTransactionCoordinator
from test_proposal_apply import _ready_transaction, _ready_two_target_transaction, _MultiCapture
from test_proposal_service import Capture, Clock


class DiskActivity:
    def __init__(self, session: Path):
        self.root = session / "activity-proof"; self.root.mkdir(mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
    def append_activity(self, activity_id, payload):
        path = self.root / f"{activity_id}.json"
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if path.exists():
            if path.read_bytes() != data: raise RuntimeError("activity contradiction")
        else:
            temporary = self.root / f".{activity_id}.{os.getpid()}"
            temporary.write_bytes(data); temporary.chmod(0o600); os.replace(temporary, path)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)
        return payload
    def verify_activity_ids(self, transaction_id, activity_ids):
        for activity_id in activity_ids:
            path = self.root / f"{activity_id}.json"
            if not path.exists() or json.loads(path.read_text())["detail"]["transaction_id"] != transaction_id:
                return False
        return bool(activity_ids)


class DiskEvents:
    def __init__(self, session: Path): self.path = session / "event-proof.jsonl"
    def append_proposal_event(self, outbox_id, payload):
        existing = self.path.read_text().splitlines() if self.path.exists() else []
        if not any(json.loads(line)["outbox_id"] == outbox_id for line in existing):
            with self.path.open("a") as stream:
                stream.write(json.dumps({"outbox_id": outbox_id, "payload": payload}, sort_keys=True) + "\n")
                stream.flush(); os.fsync(stream.fileno())
        return len(existing) + 1


def _open(session: Path, activity: DiskActivity):
    fd = os.open(session, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    return ProposalRepositoryV1.open(fs, clock=Clock(), entropy=SystemEntropy(),
                                     activity_link_verifier=activity)


def _child_apply(bundle: str, session: str, request: dict, boundary: str) -> None:
    _child_apply_key(bundle, session, request, boundary, "K" * 32)


def _child_apply_key(bundle: str, session: str, request: dict, boundary: str, key: str) -> None:
    bundle_path, session_path = Path(bundle), Path(session)
    activity = DiskActivity(session_path); repository = _open(session_path, activity)
    content = (bundle_path / "tables/orders.md").read_bytes()
    capture = Capture(content, path=bundle_path / "tables/orders.md")
    def kill(name, evidence=None):
        if name == boundary: os._exit(77)
    publication = TrustedPublicationAdapter(bundle_path, session_path / "proposal-review-v1",
                                            fault_injector=kill)
    coordinator = ProposalTransactionCoordinator(repository, capture, publication, clock=Clock(),
        activity=activity, outbox=DiskEvents(session_path), fault_injector=kill, wait_seconds=60)
    try: coordinator.apply(request, key)
    except Exception: os._exit(78)
    os._exit(0)


def _child_apply_two(bundle: str, session: str, request: dict, boundary: str) -> None:
    bundle_path, session_path = Path(bundle), Path(session); activity = DiskActivity(session_path)
    repository = _open(session_path, activity)
    contents = {name: f"---\ntype: Table\n---\n\n{name}\n".encode()
                for name in ("tables/customers", "tables/orders")}
    def kill(name, evidence=None):
        if name == boundary and ("target" not in (evidence or {})
                                 or (evidence or {}).get("target") == 0): os._exit(77)
    publication = TrustedPublicationAdapter(bundle_path, session_path / "proposal-review-v1")
    coordinator = ProposalTransactionCoordinator(repository, _MultiCapture(contents, bundle_path), publication,
        clock=Clock(), activity=activity, outbox=DiskEvents(session_path), fault_injector=kill)
    coordinator.apply(request, "K" * 32)
    os._exit(0)


def _child_recover_wal(bundle: str, session: str, done=None) -> None:
    bundle_path, session_path = Path(bundle), Path(session); activity = DiskActivity(session_path)
    repository = _open(session_path, activity)
    publication = TrustedPublicationAdapter(bundle_path, session_path / "proposal-review-v1")
    coordinator = ProposalTransactionCoordinator(repository,
        Capture(b"---\ntype: Table\n---\n\nOrders\n"), publication, clock=Clock(),
        activity=activity, outbox=DiskEvents(session_path))
    coordinator.recover_all(); publication.close()
    if done is not None: done.set()


def _child_recover_two_rollback(bundle: str, session: str, kill_after_first: bool) -> None:
    bundle_path, session_path = Path(bundle), Path(session); activity = DiskActivity(session_path)
    repository = _open(session_path, activity)
    contents = {name: f"---\ntype: Table\n---\n\n{name}\n".encode()
                for name in ("tables/customers", "tables/orders")}
    def kill(name, evidence=None):
        if (kill_after_first and name == "rollback_bitmap_advanced"
                and (evidence or {}).get("target") == 1):
            os._exit(77)
    publication = TrustedPublicationAdapter(bundle_path, session_path / "proposal-review-v1")
    ProposalTransactionCoordinator(repository, _MultiCapture(contents), publication,
        clock=Clock(), activity=activity, outbox=DiskEvents(session_path),
        fault_injector=kill).recover_all()
    publication.close()


def _hold_bundle_ownership(bundle: str, mode: str, ready, release) -> None:
    from okf_loom.io_utils import bundle_transaction_lock
    with bundle_transaction_lock(Path(bundle), mode, timeout_seconds=15):
        ready.set(); release.wait(20)


def _killed(tmp_path: Path, boundary: str):
    bundle, session, repository, coordinator, publication, request, _ = _ready_transaction(tmp_path)
    try:
        publication.close()
    finally:
        coordinator.close()
        repository.close()
    process = multiprocessing.get_context("fork").Process(
        target=_child_apply, args=(str(bundle), str(session), request, boundary))
    process.start(); process.join(30)
    assert process.exitcode == 77
    activity = DiskActivity(session); repository = _open(session, activity)
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    coordinator = ProposalTransactionCoordinator(repository,
        Capture(b"---\ntype: Table\n---\n\nOrders\n"), publication, clock=Clock(),
        activity=activity, outbox=DiskEvents(session), wait_seconds=60)
    return bundle, session, repository, coordinator, publication, request


def _recover(tmp_path: Path, boundary: str):
    values = _killed(tmp_path, boundary)
    bundle, _, repository, coordinator, publication, _ = values
    try: coordinator.recover_all()
    finally: publication.close()
    return bundle, repository


def test_crash_01_before_wal(tmp_path):
    bundle, repository = _recover(tmp_path, "transaction_allocated")
    assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()
    assert not repository.recovery_report.items


def test_crash_02_stage(tmp_path):
    bundle, repository = _recover(tmp_path, "private_files_fsynced")
    assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()
    assert not repository.recovery_report.items


def test_crash_03_prepared(tmp_path):
    bundle, repository = _recover(tmp_path, "wal_prepared")
    wal = repository.read_wal(repository.recovery_report.items[0].transaction_id) if repository.recovery_report.items else None
    assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()
    assert wal is None or wal.phase == "rolled_back"


def test_crash_04_commit_record(tmp_path):
    for index, boundary in enumerate(("before_commit_decided", "commit_decided")):
        root = tmp_path / str(index); root.mkdir()
        bundle, repository = _recover(root, boundary)
        assert (b"tags:" in (bundle / "tables/orders.md").read_bytes()) == (boundary == "commit_decided")


def test_crash_05_decided(tmp_path):
    bundle, repository = _recover(tmp_path, "commit_decided")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_crash_06_parent_fsync(tmp_path):
    bundle, repository = _recover(tmp_path, "rename_before_parent_fsync")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_crash_07_mid_publish(tmp_path):
    bundle, session, _, _, publication, request, _ = _ready_two_target_transaction(tmp_path)
    publication.close(); process = multiprocessing.get_context("fork").Process(
        target=_child_apply_two, args=(str(bundle), str(session), request, "publish_bitmap_advanced"))
    process.start(); process.join(30); assert process.exitcode == 77
    activity = DiskActivity(session); repository = _open(session, activity)
    wal = repository.read_wal(next(item.transaction_id for item in repository.recovery_report.items))
    assert wal.phase == "publishing" and wal.publish_bitmap == (True, False)
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    ProposalTransactionCoordinator(repository, _MultiCapture({name: f"---\ntype: Table\n---\n\n{name}\n".encode()
        for name in ("tables/customers", "tables/orders")}), publication, clock=Clock(), activity=activity,
        outbox=DiskEvents(session)).recover_all(); publication.close()
    assert all(b"tags:" in (bundle / f"{name}.md").read_bytes()
                 for name in ("tables/customers", "tables/orders"))


def test_wal_10_multi_target_reverse_prefix(tmp_path):
    bundle, session, _, _, publication, request, _ = _ready_two_target_transaction(tmp_path)
    originals = {name: (bundle / f"{name}.md").read_bytes()
                 for name in ("tables/customers", "tables/orders")}
    publication.close(); context = multiprocessing.get_context("fork")
    creator = context.Process(target=_child_apply_two,
        args=(str(bundle), str(session), request, "wal_prepared"))
    creator.start(); creator.join(30); assert creator.exitcode == 77
    first = context.Process(target=_child_recover_two_rollback,
                            args=(str(bundle), str(session), True))
    first.start(); first.join(30); assert first.exitcode == 77
    activity = DiskActivity(session); repository = _open(session, activity)
    transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    partial = repository.read_wal(transaction_id)
    assert partial.phase == "rolling_back" and partial.rollback_bitmap == (False, True)
    second = context.Process(target=_child_recover_two_rollback,
                             args=(str(bundle), str(session), False))
    second.start(); second.join(30); assert second.exitcode == 0
    repository = _open(session, activity); terminal = repository.read_wal(transaction_id)
    assert terminal.phase == "rolled_back" and terminal.rollback_bitmap == (True, True)
    assert all((bundle / f"{name}.md").read_bytes() == originals[name]
               for name in originals)
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    coordinator = ProposalTransactionCoordinator(repository, _MultiCapture(originals), publication,
        clock=Clock(), activity=activity, outbox=DiskEvents(session))
    try:
        with pytest.raises(Exception) as replay:
            coordinator.apply(request, "K" * 32)
        assert getattr(replay.value, "code", None) == "apply_rejected"
    finally:
        publication.close()


def test_crash_08_publish_complete(tmp_path):
    bundle, repository = _recover(tmp_path, "target_parent_fsynced")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_crash_09_finalize(tmp_path):
    for index, boundary in enumerate(("wal_finalizing", "audit_appended", "activity_appended", "outbox_appended")):
        root = tmp_path / str(index); root.mkdir()
        bundle, repository = _recover(root, boundary)
        assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_crash_10_result(tmp_path):
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    publication.close()
    transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    (session / f"proposal-review-v1/transactions/{transaction_id}/intent.json").unlink()
    activity = DiskActivity(session); reopened = _open(session, activity)
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    recovery = ProposalTransactionCoordinator(reopened, Capture(b"---\ntype: Table\n---\n\nOrders\n"),
        publication, clock=Clock(), activity=activity, outbox=DiskEvents(session))
    with pytest.raises(Exception) as blocked:
        recovery.recover_all()
    assert getattr(blocked.value, "code", None) == "transaction_recovering"
    assert reopened.read_wal(transaction_id).phase == "indeterminate"
    publication.close()


def test_crash_11_result_wal(tmp_path):
    bundle, repository = _recover(tmp_path, "endpoint_terminal")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_crash_12_outbox_pending(tmp_path):
    bundle, repository = _recover(tmp_path, "wal_terminal")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_crash_13_event_receipt(tmp_path):
    bundle, repository = _recover(tmp_path, "event_appended")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_crash_14_response_loss(tmp_path):
    bundle, repository = _recover(tmp_path, "receipt_updated")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_crash_15_rollback(tmp_path):
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "wal_prepared")
    publication.close()
    def kill(name, evidence=None):
        if name == "rollback_bitmap_advanced": os._exit(77)
    process = multiprocessing.get_context("fork").Process(
        target=_recover_child, args=(str(bundle), str(session), "rollback_bitmap_advanced"))
    process.start(); process.join(30); assert process.exitcode == 77
    bundle, repository = _recover_existing(bundle, session)
    assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()


def _recover_child(bundle: str, session: str, boundary: str):
    bundle_path, session_path = Path(bundle), Path(session); activity = DiskActivity(session_path)
    repository = _open(session_path, activity)
    def kill(name, evidence=None):
        if name == boundary: os._exit(77)
    publication = TrustedPublicationAdapter(bundle_path, session_path / "proposal-review-v1", fault_injector=kill)
    ProposalTransactionCoordinator(repository, Capture(b"---\ntype: Table\n---\n\nOrders\n"), publication,
        clock=Clock(), activity=activity, outbox=DiskEvents(session_path), fault_injector=kill).recover_all()


def _recover_existing(bundle: Path, session: Path, *, indeterminate: bool = False):
    activity = DiskActivity(session); repository = _open(session, activity)
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    coordinator = ProposalTransactionCoordinator(repository, Capture(b"---\ntype: Table\n---\n\nOrders\n"), publication,
        clock=Clock(), activity=activity, outbox=DiskEvents(session))
    if indeterminate:
        with pytest.raises(Exception) as blocked: coordinator.recover_all()
        assert getattr(blocked.value, "code", None) == "transaction_recovering"
    else: coordinator.recover_all()
    publication.close(); return bundle, repository


def test_crash_16_indeterminate(tmp_path):
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    publication.close(); transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    stage = next((session / f"proposal-review-v1/transactions/{transaction_id}/stage").iterdir())
    stage.unlink(); stage.write_bytes(b"foreign"); stage.chmod(0o600)
    bundle, repository = _recover_existing(bundle, session, indeterminate=True)
    assert repository.read_wal(transaction_id).phase == "indeterminate"


def test_concurrent_same_wal_recovery_finalizes_once(tmp_path):
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    publication.close(); context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_child_recover_wal, args=(str(bundle), str(session)))
               for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(30)
    assert [worker.exitcode for worker in workers] == [0, 0]
    reopened = _open(session, DiskActivity(session))
    audits = [row for row in reopened.snapshot().aggregate("transactions")
              if row.kind == "transaction" and row.payload.get("operation") == "apply"]
    assert len(audits) == 1
    assert not [row for row in reopened.snapshot().aggregate("corruption")
                if row.kind == "corruption"]


def test_crash_18_complete_retry_lock_rank(tmp_path):
    bundle, session, repository, coordinator, publication, request = _killed(
        tmp_path, "commit_decided")
    trace = []
    original = repository.transaction_session
    class TracedSession:
        def __enter__(self):
            self.inner = original(); self.inner.__enter__(); return self
        def __exit__(self, *args): return self.inner.__exit__(*args)
        def ranked(self, name):
            trace.append(name); return self.inner.ranked(name)
        def locked(self, name, identity):
            trace.append(name); return self.inner.locked(name, identity)
    repository.transaction_session = TracedSession
    try:
        result = coordinator.apply(request, "K" * 32)
        assert result.status == "applied"
        assert trace == ["bundle", "concept", "lineage", "endpoint", "transaction",
                         "history", "activity_outbox"]
        audits = [row for row in repository.snapshot().aggregate("transactions")
                  if row.kind == "transaction" and row.payload.get("transaction_id") == result.transaction_id]
        assert len(audits) == 1
    finally:
        publication.close()


def test_terminal_startup_corruption_writes_marker_before_blocking(tmp_path):
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    result = coordinator.apply(request, "K" * 32); publication.close()
    intent = session / f"proposal-review-v1/transactions/{result.transaction_id}/intent.json"
    intent.write_bytes(b'{"corrupt":true}'); intent.chmod(0o600)
    disk_activity = DiskActivity(session); reopened = _open(session, disk_activity)
    recovered_publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    recovery = ProposalTransactionCoordinator(reopened,
        Capture(b"---\ntype: Table\n---\n\nOrders\n"), recovered_publication,
        clock=Clock(), activity=disk_activity, outbox=DiskEvents(session))
    try:
        with pytest.raises(Exception):
            recovery.recover_all()
        markers = [row.payload for row in reopened.snapshot().aggregate("corruption")
                   if row.kind == "corruption"
                   and (row.payload.get("authority_identity") or {}).get("transaction_id")
                   == result.transaction_id]
        assert len(markers) == 1 and not reopened.recovery_report.serving_safe
    finally:
        recovered_publication.close()


@pytest.mark.parametrize("mode", ["exclusive", "shared"])
def test_recovery_serializes_with_direct_writer_and_shared_capture(tmp_path, mode):
    root = tmp_path / mode; root.mkdir()
    bundle, session, repository, coordinator, publication, request = _killed(root, "commit_decided")
    publication.close(); context = multiprocessing.get_context("fork")
    ready, release, done = context.Event(), context.Event(), context.Event()
    owner = context.Process(target=_hold_bundle_ownership,
                            args=(str(bundle), mode, ready, release))
    owner.start(); assert ready.wait(15)
    recovery = context.Process(target=_child_recover_wal,
                               args=(str(bundle), str(session), done))
    recovery.start(); assert not done.wait(1)
    release.set(); owner.join(20); recovery.join(30)
    assert owner.exitcode == 0 and recovery.exitcode == 0 and done.is_set()
