"""RESOURCE-01..04 hard repository/service authority proofs."""
from __future__ import annotations

import os
import multiprocessing
import time
import threading
import dataclasses
import shutil
from types import MappingProxyType

import pytest

from okf_loom.proposal_review.errors import LockUnavailableError, RepositoryError, SchemaError
from okf_loom.proposal_review.ports import LinuxDirFdFilesystem, RuntimeIdentities
from okf_loom.proposal_review.repository import ProposalRepositoryV1, RepositoryLimits
from okf_loom.proposal_review.service_contracts import CreateProposalInput, DecisionInput
from test_proposal_service import Clock, Entropy
from test_proposal_service import Capture, RUNTIME
from okf_loom.proposal_review.service import ProposalService
from okf_loom.proposal_review import service as service_module
from okf_loom.proposal_review import repository as repository_module
from okf_loom.proposal_review.ports import SystemEntropy
from test_proposal_service_recovery import _adapter, _hold_repository_lock, _request


def _lease_worker(root: str, start, results) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=SystemEntropy())
    start.wait(5)
    try:
        lease = repository.acquire_preview_lease(Clock().now(), timeout_seconds=10)
        results.put("acquired")
        repository.release_preview_lease(lease)
    except RepositoryError as exc:
        results.put(exc.code)


def _rate_worker(root: str, start, results) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=SystemEntropy())
    start.wait(5)
    try:
        repository.admit_create(Clock().now()); results.put("ok")
    except RepositoryError as exc:
        results.put(exc.code)


def _lease_crash_worker(root: str) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=SystemEntropy())
    repository.acquire_preview_lease(Clock().now(), timeout_seconds=60)
    os._exit(81)


def _repo(tmp_path):
    root = tmp_path / "resources"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    return ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())


def test_resource_01_preparse_caps() -> None:
    with pytest.raises(SchemaError) as create:
        CreateProposalInput.from_mapping(b"{" + b" " * 1_048_576)
    assert create.value.code == "body_too_large"
    with pytest.raises(SchemaError) as decision:
        DecisionInput.from_mapping(b"{" + b" " * 65_536)
    assert decision.value.code == "body_too_large"


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_resource_02_locked_limits(tmp_path) -> None:
    repository = _repo(tmp_path)
    context = multiprocessing.get_context("spawn"); start = context.Event(); results = context.Queue()
    root = str((tmp_path / "resources").resolve())
    workers = [context.Process(target=_rate_worker, args=(root, start, results)) for _ in range(4)]
    for worker in workers: worker.start()
    start.set()
    for worker in workers: worker.join(10); assert worker.exitcode == 0
    observed = [results.get(timeout=2) for _ in workers]
    assert observed.count("ok") == repository.limits.create_rate_burst
    assert observed.count("create_rate_limited") == 1

    def bounded(name, limits):
        root_path = tmp_path / name; root_path.mkdir(mode=0o700); os.chmod(root_path, 0o700)
        repo = ProposalRepositoryV1.bootstrap(_adapter(root_path), clock=Clock(),
                                              entropy=SystemEntropy(), limits=limits)
        capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
        svc = ProposalService(repo, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
        return repo, capture, svc

    limits = dataclasses.replace(RepositoryLimits(), active_proposals_per_session=1,
                                 lineages_per_session=2)
    _, capture, svc = bounded("active", limits)
    svc.create(_request(capture), idempotency_key="A" * 32)
    with pytest.raises(RepositoryError): svc.create(_request(capture), idempotency_key="B" * 32)

    limits = dataclasses.replace(RepositoryLimits(), lineages_per_session=1,
                                 active_proposals_per_session=2)
    _, capture, svc = bounded("lineage", limits)
    svc.create(_request(capture), idempotency_key="C" * 32)
    with pytest.raises(RepositoryError): svc.create(_request(capture), idempotency_key="D" * 32)

    limits = dataclasses.replace(RepositoryLimits(), revisions_per_lineage=1)
    _, capture, svc = bounded("revision", limits)
    created = svc.create(_request(capture), idempotency_key="E" * 32).data
    supersede = _request(capture); supersede["proposal_id"] = created["proposal"]["proposal_id"]
    supersede["supersedes_revision"] = 1
    with pytest.raises(RepositoryError): svc.create(supersede, idempotency_key="F" * 32)

    limits = dataclasses.replace(RepositoryLimits(), repository_quota_bytes=1)
    quota_repo, capture, svc = bounded("quota", limits)
    with pytest.raises(RepositoryError) as quota:
        svc.create(_request(capture), idempotency_key="G" * 32)
    assert quota.value.code == "repository_quota_exceeded"
    assert quota_repo.snapshot().service_intents == ()


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_resource_03_preview_bounds(tmp_path) -> None:
    repository = _repo(tmp_path)
    first = repository.acquire_preview_lease(Clock().now(), timeout_seconds=1)
    second = repository.acquire_preview_lease(Clock().now(), timeout_seconds=1)
    assert first != second
    context = multiprocessing.get_context("spawn"); start = context.Event(); results = context.Queue()
    root = str((tmp_path / "resources").resolve())
    workers = [context.Process(target=_lease_worker, args=(root, start, results)) for _ in range(8)]
    for worker in workers: worker.start()
    start.set()
    deadline = time.monotonic() + 5
    queued = 0
    while time.monotonic() < deadline:
        with repository._lock.hold(repository._lock_timeout):
            queued = len(repository._read_admission_locked(Clock().now())["preview_queue"])
            if queued == 8:
                break
        time.sleep(0.01)
    assert queued == 8
    with pytest.raises(RepositoryError) as full:
        repository.acquire_preview_lease(Clock().now(), timeout_seconds=.1)
    assert full.value.code == "preview_concurrency_limited"
    repository.release_preview_lease(first); repository.release_preview_lease(second)
    for worker in workers: worker.join(10); assert worker.exitcode == 0
    assert [results.get(timeout=2) for _ in workers].count("acquired") == 8


@pytest.mark.parametrize("late_error", [False, True], ids=["late-success", "late-error"])
def test_resource_03_timeout_cancel_failure_keeps_actual_worker_lease(tmp_path, late_error) -> None:
    repository = _repo(tmp_path)
    service = ProposalService(repository, Capture(b"---\ntype: Table\n---\n\nOrders\n"),
        Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    started = threading.Event(); finish = threading.Event()
    def work():
        started.set(); finish.wait(5)
        if late_error: raise RuntimeError("late pure failure")
        return "late pure success"
    lease = repository.acquire_preview_lease(Clock().now(), timeout_seconds=1)
    with pytest.raises(RepositoryError) as timed_out:
        service._run_preview_job(work, lease_id=lease, deadline=time.monotonic() + .02)
    assert timed_out.value.code == "preview_timeout" and started.is_set()
    with repository._lock.hold(repository._lock_timeout):
        assert len(repository._read_admission_locked(Clock().now())["preview_leases"]) == 1
    finish.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with repository._lock.hold(repository._lock_timeout):
            active = len(repository._read_admission_locked(Clock().now())["preview_leases"])
        if active == 0: break
        time.sleep(.01)
    assert active == 0


def test_resource_03_repeated_timeouts_never_exceed_two_actual_workers(tmp_path) -> None:
    repository = _repo(tmp_path)
    service = ProposalService(repository, Capture(b"---\ntype: Table\n---\n\nOrders\n"),
        Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    finish = threading.Event()
    baseline_fds = len(os.listdir("/proc/self/fd"))
    for _ in range(2):
        lease = repository.acquire_preview_lease(Clock().now(), timeout_seconds=1)
        with pytest.raises(RepositoryError) as timed_out:
            service._run_preview_job(lambda: finish.wait(5), lease_id=lease,
                                     deadline=time.monotonic() + .02)
        assert timed_out.value.code == "preview_timeout"
    with repository._lock.hold(repository._lock_timeout):
        assert len(repository._read_admission_locked(Clock().now())["preview_leases"]) == 2
    assert len([t for t in threading.enumerate() if t.name.startswith("proposal-preview_")]) <= 2
    assert len([t for t in threading.enumerate()
                if t.name == "proposal-preview-heartbeat"]) <= 2
    with pytest.raises(RepositoryError) as queued_timeout:
        repository.acquire_preview_lease(Clock().now(), timeout_seconds=.02)
    assert queued_timeout.value.code == "preview_timeout"
    finish.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with repository._lock.hold(repository._lock_timeout):
            active = len(repository._read_admission_locked(Clock().now())["preview_leases"])
        threads = [t for t in threading.enumerate() if t.name.startswith("proposal-preview")]
        if active == 0 and not threads: break
        time.sleep(.01)
    assert active == 0 and not threads
    assert len(os.listdir("/proc/self/fd")) <= baseline_fds + 2


def test_service_close_joins_preview_executor_and_heartbeat_and_releases_root(tmp_path) -> None:
    repository = _repo(tmp_path)
    service = ProposalService(repository, Capture(b"---\ntype: Table\n---\n\nOrders\n"),
        Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    lease = repository.acquire_preview_lease(Clock().now(), timeout_seconds=1)
    started = threading.Event(); finish = threading.Event(); outcome = []
    def work():
        started.set(); finish.wait(5); return "done"
    caller = threading.Thread(target=lambda: outcome.append(
        service._run_preview_job(work, lease_id=lease, deadline=time.monotonic() + 5)))
    caller.start(); assert started.wait(2)
    with service._lifecycle_lock:
        owned_executors = tuple(service._executors)
        owned_heartbeats = tuple(thread for _stop, thread in service._heartbeats)
    assert len(owned_executors) == len(owned_heartbeats) == 1
    owned_executor_threads = tuple(thread for executor in owned_executors
                                   for thread in executor._threads)
    repository_fd = repository._fs._fd
    assert repository_fd >= 0 and all(thread.is_alive() for thread in owned_heartbeats)
    closer = threading.Thread(target=service.close)
    closer.start(); time.sleep(.05)
    assert closer.is_alive()
    finish.set(); caller.join(2); closer.join(2)
    assert outcome == ["done"] and not caller.is_alive() and not closer.is_alive()
    assert repository._fs._fd == -1
    assert not [thread for thread in (*owned_heartbeats, *owned_executor_threads)
                if thread.is_alive()]
    with service._lifecycle_lock:
        assert not service._executors and not service._heartbeats
    root = tmp_path / "resources"; moved = tmp_path / "resources-moved"
    root.rename(moved); shutil.rmtree(moved)
    assert not moved.exists()


def test_resource_03_process_death_lease_expires(tmp_path) -> None:
    repository = _repo(tmp_path); root = str((tmp_path / "resources").resolve())
    process = multiprocessing.get_context("spawn").Process(target=_lease_crash_worker, args=(root,))
    process.start(); process.join(10); assert process.exitcode == 81
    class LaterClock:
        def now(self): return "2026-07-14T12:35:57.789Z"
    object.__setattr__(repository, "_clock", LaterClock())
    with repository._lock.hold(repository._lock_timeout):
        body = repository._read_admission_locked(LaterClock().now())
    assert body["preview_leases"] == []


def test_resource_03_heartbeat_keeps_late_live_workers_at_capacity(tmp_path, monkeypatch) -> None:
    class MovingClock:
        value = "2026-07-14T12:34:56.789Z"
        def now(self): return self.value
    clock = MovingClock(); root = tmp_path / "heartbeat"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=clock, entropy=SystemEntropy())
    service = ProposalService(repository, Capture(b"---\ntype: Table\n---\n\nOrders\n"),
        clock, RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    monkeypatch.setattr(service_module, "PREVIEW_HEARTBEAT_INTERVAL_SECONDS", .01)
    finish = threading.Event()
    for _ in range(2):
        lease = repository.acquire_preview_lease(clock.now(), timeout_seconds=1)
        with pytest.raises(RepositoryError):
            service._run_preview_job(lambda: finish.wait(5), lease_id=lease,
                                     deadline=time.monotonic() + .02)
    clock.value = "2026-07-14T12:35:55.789Z"
    deadline = time.monotonic() + 1
    renewed = False
    while time.monotonic() < deadline:
        with repository._lock.hold(repository._lock_timeout):
            leases = repository._read_admission_locked(clock.now())["preview_leases"]
        renewed = len(leases) == 2 and all(
            lease["heartbeat_expires_at"] > "2026-07-14T12:36:00.000Z"
            and (lease["owner_pid"], lease["owner_start_token"])
                == repository.preview_owner_process_id
            for lease in leases)
        if renewed: break
        time.sleep(.01)
    assert renewed
    clock.value = "2026-07-14T12:35:57.789Z"  # beyond each original expiry
    with pytest.raises(RepositoryError) as third:
        repository.acquire_preview_lease(clock.now(), timeout_seconds=.02)
    assert third.value.code == "preview_timeout"
    finish.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with repository._lock.hold(repository._lock_timeout):
            active = repository._read_admission_locked(clock.now())["preview_leases"]
        if not active: break
        time.sleep(.01)
    assert active == []


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_resource_03_live_identity_survives_real_heartbeat_lock_failure(
        tmp_path, monkeypatch) -> None:
    class MovingClock:
        value = "2026-07-14T12:34:56.789Z"
        def now(self): return self.value
    clock = MovingClock(); root = tmp_path / "heartbeat-lock"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=clock, entropy=SystemEntropy())
    service = ProposalService(repository, Capture(b"---\ntype: Table\n---\n\nOrders\n"),
        clock, RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    monkeypatch.setattr(service_module, "PREVIEW_HEARTBEAT_INTERVAL_SECONDS", .01)
    finish = threading.Event(); fault_phase = threading.Event()
    failures = {}; released = {}
    original_renew = repository.renew_preview_lease
    original_release = repository.release_preview_lease
    normal_lock_timeout = repository._lock_timeout

    def observe_renew(lease_id):
        # Do not start a normal-budget renewal that could straddle fault setup.
        if not fault_phase.is_set():
            return True
        try:
            return original_renew(lease_id)
        except LockUnavailableError:
            failures[lease_id].set()
            raise

    def observe_release(lease_id):
        original_release(lease_id)
        if lease_id in released:  # Admission also releases its timed-out queue entry.
            released[lease_id].set()  # Only a successful real release counts.

    monkeypatch.setattr(repository, "renew_preview_lease", observe_renew)
    monkeypatch.setattr(repository, "release_preview_lease", observe_release)
    context = multiprocessing.get_context("spawn"); ready = context.Event(); release = context.Event()
    holder = context.Process(target=_hold_repository_lock, args=(str(root), ready, release))
    try:
        for _ in range(2):
            lease = repository.acquire_preview_lease(clock.now(), timeout_seconds=1)
            failures[lease] = threading.Event(); released[lease] = threading.Event()
            with pytest.raises(RepositoryError) as timed_out:
                service._run_preview_job(finish.wait, lease_id=lease,
                                         deadline=time.monotonic() + .02)
            assert timed_out.value.code == "preview_timeout"
        holder.start(); assert ready.wait(10)
        repository._lock_timeout = .05
        fault_phase.set()
        assert all(event.wait(2) for event in failures.values())
        # Stop AND join: replacing method lookup alone leaves in-flight originals
        # able to renew against the advanced clock and invalidate the expiry proof.
        with service._lifecycle_lock:
            heartbeats = tuple(service._heartbeats)
        assert len(heartbeats) == 2
        for stop, _thread in heartbeats:
            stop.set()
        for _stop, thread in heartbeats:
            thread.join(2)
            assert not thread.is_alive()
        repository._lock_timeout = normal_lock_timeout
        clock.value = "2026-07-14T12:35:57.789Z"
        release.set(); holder.join(20); assert holder.exitcode == 0
        with repository._lock.hold(repository._lock_timeout):
            leases = repository._read_admission_locked(clock.now())["preview_leases"]
        assert len(leases) == 2
        assert {lease["lease_id"] for lease in leases} == set(failures)
        state, start_token = repository_module._linux_process_identity(os.getpid())
        assert state == "alive" and start_token is not None
        assert repository.preview_owner_process_id == (os.getpid(), start_token)
        assert all(lease["heartbeat_expires_at"] < clock.now()
                   and (lease["owner_pid"], lease["owner_start_token"])
                       == repository.preview_owner_process_id for lease in leases)
        with pytest.raises(RepositoryError) as third:
            repository.acquire_preview_lease(clock.now(), timeout_seconds=.02)
        assert third.value.code == "preview_timeout"
        finish.set()
        assert all(event.wait(normal_lock_timeout + 2) for event in released.values())
        with repository._lock.hold(repository._lock_timeout):
            body = repository._read_admission_locked(clock.now())
            assert body["preview_leases"] == []
            assert body["preview_queue"] == []

        # PID reuse cannot inherit an expired lease with a different start token.
        with repository._lock.hold(repository._lock_timeout):
            body = repository._read_admission_locked(clock.now())
            body["preview_leases"] = [{"lease_id": repository.new_id("svc_"),
                "owner_pid": os.getpid(), "owner_start_token": "0",
                "heartbeat_expires_at": "2026-07-14T12:35:56.789Z"}]
            repository._write_admission_locked(body)
            assert repository._read_admission_locked(clock.now())["preview_leases"] == []
            body = repository._read_admission_locked(clock.now())
            body["preview_leases"] = [{"lease_id": repository.new_id("svc_"),
                "owner_pid": 999999, "owner_start_token": "1",
                "heartbeat_expires_at": "2026-07-14T12:35:56.789Z"}]
            repository._write_admission_locked(body)
            monkeypatch.setattr(repository_module, "_linux_process_identity",
                                lambda pid: ("unknown", None))
            assert len(repository._read_admission_locked(clock.now())["preview_leases"]) == 1
    finally:
        repository._lock_timeout = normal_lock_timeout
        release.set()
        if holder.pid is not None:
            holder.join(20)
            if holder.is_alive():
                holder.terminate(); holder.join(5)
        finish.set()
        service.close()


def test_resource_04_list_projection(tmp_path) -> None:
    root = tmp_path / "list-bound"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    service.create(_request(capture), idempotency_key="Q" * 32)
    snapshot = repository.snapshot(); proposal = snapshot.aggregate("proposals")[0]
    records = dict(snapshot.records)
    bound = (repository.limits.lineages_per_session *
             (repository.limits.revisions_per_lineage + 1))
    records["proposals"] = (proposal,) * (bound + 1)
    oversized = dataclasses.replace(snapshot, records=MappingProxyType(records))
    original_snapshot = repository.snapshot
    repository.snapshot = lambda: oversized
    repository.get_preview = lambda digest: (_ for _ in ()).throw(
        AssertionError("bounded metadata rejection must precede preview materialization"))
    try:
        with pytest.raises(RepositoryError) as caught:
            service.list({"limit": 50})
        assert caught.value.code == "storage_corrupt"
    finally:
        repository.snapshot = original_snapshot
