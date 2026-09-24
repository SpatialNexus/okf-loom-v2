"""SERVICE-01..08 exact intent/restart fault proofs.

ASYNC_LIFECYCLE_MATRIX columns exercised here:
operation | persisted phase | public domain result | late completion | retry |
cleanup | exact test.  Create/supersede/decision/withdraw use prepared →
domain_appended → outbox_appended → terminal → committed; late completion is
repository-owned, same-key retry reconciles before CAS, and cleanup preserves
nonterminal/referenced preview authority. Outbox append-before-mark is covered
by ``tests/test_proposal_events.py``.
"""
from __future__ import annotations

import multiprocessing
import os
import copy
import time
from pathlib import Path

import pytest

from okf_loom.proposal_review.errors import ProposalReviewError
from okf_loom.proposal_review.ports import (LinuxDirFdFilesystem, RuntimeIdentities,
                                            SystemEntropy)
from okf_loom.proposal_review.repository import ProposalRepositoryV1
from okf_loom.proposal_review.service import ProposalService
from test_proposal_service import Capture, Clock, Entropy, RUNTIME


def _adapter(path: Path) -> LinuxDirFdFilesystem:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: return LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)


def _request(capture: Capture) -> dict:
    revision = capture.snapshot.concepts[0].revision
    return {"schema": 1, "mode": "proposal", "author": "agent",
        "summary": "Tag orders", "rationale": "Review exact recovery.",
        "expected_snapshot": capture.snapshot.to_mapping(),
        "operations": [{"schema": 1, "op_id": "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
            "kind": "set_frontmatter", "target": "tables/orders",
            "args": {"key": "tags", "value": ["orders"]},
            "reads": ["tables/orders"], "writes": ["tables/orders"],
            "expected_revisions": {"tables/orders": revision},
            "depends_on": [], "extensions": {}}], "provenance": [], "extensions": {}}


def _crash_worker(path: str, boundary: str) -> None:
    fs = _adapter(Path(path))
    def fault(observed: str) -> None:
        if observed == boundary:
            os._exit(77)
    repository = ProposalRepositoryV1.bootstrap(
        fs, clock=Clock(), entropy=Entropy(), fault_injector=fault)
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(),
        RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    service.create(_request(capture), idempotency_key="R" * 32)


@pytest.mark.parametrize("boundary", [
    "service_preview_staged", "service_intent_prepared", "service_endpoint_prepared",
    "service_preview_promoted", "service_domain_appended",
    "service_intent_domain_appended", "service_outbox_appended",
    "service_intent_outbox_appended", "service_terminal_appended",
    "service_intent_terminal", "service_intent_committed",
])
def test_service_01_create_wal(tmp_path: Path, boundary: str) -> None:
    root = tmp_path / "session"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_worker, args=(str(root), boundary))
    process.start(); process.join(20)
    assert process.exitcode == 77
    fs = _adapter(root)
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(),
        RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    first = service.create(_request(capture), idempotency_key="R" * 32).to_mapping()
    second = service.create(_request(capture), idempotency_key="R" * 32).to_mapping()
    assert first == second
    proposals = [w for w in repository.snapshot().aggregate("proposals") if w.kind == "proposal"]
    assert len(proposals) == 1


def test_service_03_decision_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "decision"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=Entropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    created = service.create(_request(capture), idempotency_key="C" * 32).data
    pid = created["proposal"]["proposal_id"]
    body = {"schema": 1, "decision": "accepted", "reason": "",
            "previous_row_token": None, "read_model_token": created["read_model_token"]}
    first = service.decide(pid, 1, "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", body,
                           idempotency_key="D" * 32).to_mapping()
    body["read_model_token"] = "sha256:" + "f" * 64
    second = service.decide(pid, 1, "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", body,
                            idempotency_key="D" * 32).to_mapping()
    assert second == first


def _supersede_worker(path: str, proposal_id: str, key: str, start, results) -> None:
    repository = ProposalRepositoryV1.bootstrap(
        _adapter(Path(path)), clock=Clock(), entropy=SystemEntropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    body = _request(capture); body.update(proposal_id=proposal_id, supersedes_revision=1)
    start.wait(10)
    try:
        value = service.create(body, idempotency_key=key).data
        results.put(("ok", value["proposal"]["proposal_revision"]))
    except ProposalReviewError as exc:
        results.put((getattr(exc, "code", type(exc).__name__), None))


def _same_key_worker(path: str, start, results, key: str = "S" * 32) -> None:
    repository = ProposalRepositoryV1.bootstrap(
        _adapter(Path(path)), clock=Clock(), entropy=SystemEntropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    start.wait(10)
    try:
        value = service.create(_request(capture), idempotency_key=key).data
        results.put(("ok", value["proposal"]["proposal_id"], value["read_model_token"]))
    except ProposalReviewError as exc:
        results.put((exc.code, None, None))


def test_service_02_supersede_uniqueness(tmp_path: Path) -> None:
    root = tmp_path / "race"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    pid = service.create(_request(capture), idempotency_key="B" * 32).data["proposal"]["proposal_id"]
    context = multiprocessing.get_context("spawn"); start = context.Event(); results = context.Queue()
    workers = [context.Process(target=_supersede_worker,
                args=(str(root), pid, key * 32, start, results)) for key in ("X", "Y")]
    for worker in workers: worker.start()
    start.set()
    for worker in workers: worker.join(20); assert worker.exitcode == 0
    observed = [results.get(timeout=2), results.get(timeout=2)]
    assert sum(item[0] == "ok" for item in observed) == 1
    assert any(item[0] in {"invalid_state", "indeterminate_outcome"} for item in observed)
    reopened = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    revisions = [w.payload["proposal_revision"] for w in reopened.snapshot().aggregate("proposals")
                 if w.kind == "proposal" and w.payload["proposal_id"] == pid]
    assert sorted(revisions) == [1, 2]


def test_service_06_reconcile_precedence(tmp_path: Path) -> None:
    root = tmp_path / "same-key"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    # Establish repository root before workers race its mutation protocol.
    ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    context = multiprocessing.get_context("spawn"); start = context.Event(); results = context.Queue()
    workers = [context.Process(target=_same_key_worker, args=(str(root), start, results))
               for _ in range(2)]
    for worker in workers: worker.start()
    start.set()
    for worker in workers: worker.join(20); assert worker.exitcode == 0
    observed = [results.get(timeout=2), results.get(timeout=2)]
    assert observed[0][0] == observed[1][0] == "ok"
    assert observed[0][1] == observed[1][1]
    reopened = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    assert len([w for w in reopened.snapshot().aggregate("proposals") if w.kind == "proposal"]) == 1


def test_concurrent_unrelated_creates_keep_proposal_specific_tokens(tmp_path: Path) -> None:
    root = tmp_path / "unrelated"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    context = multiprocessing.get_context("spawn"); start = context.Event(); results = context.Queue()
    workers = [context.Process(target=_same_key_worker,
        args=(str(root), start, results, key * 32)) for key in ("U", "V")]
    for worker in workers: worker.start()
    start.set()
    for worker in workers: worker.join(20); assert worker.exitcode == 0
    observed = [results.get(timeout=2), results.get(timeout=2)]
    assert all(item[0] == "ok" for item in observed)
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    reader = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                             proposal_capability_active=True)
    for _, proposal_id, returned_token in observed:
        assert reader.show(proposal_id, 1).data["read_model_token"] == returned_token


def test_long_lived_show_and_list_rescan_cross_process_authority(tmp_path: Path) -> None:
    root = tmp_path / "long-lived"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    old_repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    old_service = ProposalService(old_repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                                  proposal_capability_active=True)
    writer_repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    writer = ProposalService(writer_repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                             proposal_capability_active=True)
    created = writer.create(_request(capture), idempotency_key="L" * 32).data
    proposal_id = created["proposal"]["proposal_id"]
    assert old_service.show(proposal_id, 1).data["read_model_token"] == created["read_model_token"]
    listed = old_service.list({"state": None, "limit": 50, "cursor": None}).data
    assert [item["proposal_id"] for item in listed["items"]] == [proposal_id]


def test_service_08_preview_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "preview-cleanup"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_worker, args=(str(root), "service_preview_staged"))
    process.start(); process.join(20); assert process.exitcode == 77
    fs = _adapter(root)
    repo_root = fs.subroot("proposal-review-v1")
    staged = repo_root.list_dir("preview-staging")
    assert len(staged) == 1
    class LaterClock:
        def now(self): return "2026-08-20T12:34:56.789Z"
    ProposalRepositoryV1.bootstrap(fs, clock=LaterClock(), entropy=SystemEntropy())
    assert not repo_root.exists(f"preview-staging/{staged[0]}/preview.json")


def test_service_07_startup_recovery(tmp_path: Path) -> None:
    root = tmp_path / "recovery-attempts"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_worker, args=(str(root), "service_intent_prepared"))
    process.start(); process.join(20); assert process.exitcode == 77
    fs = _adapter(root); repo_root = fs.subroot("proposal-review-v1")
    staged_id = repo_root.list_dir("preview-staging")[0]
    repo_root.unlink(f"preview-staging/{staged_id}/preview.json")
    for _ in range(100):
        ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=SystemEntropy())
    intent_name = repo_root.list_dir("service-wal")[0]
    from okf_loom.proposal_review import schemas
    intent = schemas.ServiceMutationIntent.from_json(repo_root.read_bytes(f"service-wal/{intent_name}"))
    assert intent.phase == "indeterminate" and intent.recovery_attempts == 100


_METADATA_PHASES = ["service_intent_prepared", "service_endpoint_prepared",
    "service_domain_appended", "service_intent_domain_appended",
    "service_outbox_appended", "service_intent_outbox_appended",
    "service_terminal_appended", "service_intent_terminal", "service_intent_committed"]


def _mutation_crash_worker(path: str, boundary: str, operation: str,
                           proposal_id: str, token: str) -> None:
    fs = _adapter(Path(path))
    def fault(observed: str) -> None:
        if observed == boundary: os._exit(78)
    repository = ProposalRepositoryV1.bootstrap(
        fs, clock=Clock(), entropy=SystemEntropy(), fault_injector=fault)
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    if operation in {"decision", "keyless"}:
        service.decide(proposal_id, 1, "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
            {"schema": 1, "decision": "accepted", "reason": "",
             "previous_row_token": None, "read_model_token": token},
            idempotency_key=None if operation == "keyless" else "K" * 32)
    else:
        service.withdraw(proposal_id, 1,
            {"schema": 1, "read_model_token": token, "reason": "stop"},
            idempotency_key="W" * 32)


def _hold_repository_lock(path: str, ready, release) -> None:
    repository = ProposalRepositoryV1.bootstrap(
        _adapter(Path(path)), clock=Clock(), entropy=SystemEntropy())
    with repository._lock.hold(repository._lock_timeout):
        ready.set(); release.wait(10)


def _base_for_mutation(root: Path):
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    created = service.create(_request(capture), idempotency_key="M" * 32).data
    return capture, created["proposal"]["proposal_id"], created["read_model_token"]


def _race_mutation_worker(path: str, start, results, operation: str,
                          proposal_id: str, token: str, op_id: str | None = None) -> None:
    repository = ProposalRepositoryV1.bootstrap(_adapter(Path(path)), clock=Clock(), entropy=SystemEntropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    start.wait(10)
    try:
        if operation == "decision":
            value = service.decide(proposal_id, 1, op_id,
                {"schema": 1, "decision": "accepted", "reason": "",
                 "previous_row_token": None, "read_model_token": token},
                idempotency_key=("A" if op_id.endswith("XYZ") else "B") * 32).data
        else:
            value = service.withdraw(proposal_id, 1,
                {"schema": 1, "read_model_token": token, "reason": "race"},
                idempotency_key="Z" * 32).data
        results.put(("ok", value["read_model_token"]))
    except ProposalReviewError as exc:
        results.put((exc.code, None))


def test_cross_process_decisions_on_different_ops_are_cas_serialized(tmp_path: Path) -> None:
    root = tmp_path / "decision-race"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    request = _request(capture); second = copy.deepcopy(request["operations"][0])
    second["op_id"] = "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYA"; second["args"] = {"key": "owner", "value": "team"}
    second["depends_on"] = ["op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"]
    request["operations"].append(second)
    creator = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    created = creator.create(request, idempotency_key="T" * 32).data
    proposal_id, token = created["proposal"]["proposal_id"], created["read_model_token"]
    context = multiprocessing.get_context("spawn"); start = context.Event(); results = context.Queue()
    workers = [context.Process(target=_race_mutation_worker,
        args=(str(root), start, results, "decision", proposal_id, token, op_id))
        for op_id in ("op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ", "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYA")]
    for worker in workers: worker.start()
    start.set()
    for worker in workers: worker.join(20); assert worker.exitcode == 0
    observed = [results.get(timeout=2), results.get(timeout=2)]
    assert sorted(item[0] for item in observed) == ["ok", "stale_read_model"]
    reopened = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    reader = ProposalService(reopened, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                             proposal_capability_active=True)
    assert reader.show(proposal_id, 1).data["read_model_token"] == next(
        item[1] for item in observed if item[0] == "ok")


def test_cross_process_decision_vs_withdraw_has_one_authoritative_winner(tmp_path: Path) -> None:
    root = tmp_path / "decision-withdraw"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    capture, proposal_id, token = _base_for_mutation(root)
    context = multiprocessing.get_context("spawn"); start = context.Event(); results = context.Queue()
    workers = [context.Process(target=_race_mutation_worker,
        args=(str(root), start, results, operation, proposal_id, token,
              "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"))
        for operation in ("decision", "withdraw")]
    for worker in workers: worker.start()
    start.set()
    for worker in workers: worker.join(20); assert worker.exitcode == 0
    observed = [results.get(timeout=2), results.get(timeout=2)]
    assert sum(item[0] == "ok" for item in observed) == 1
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    reader = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                             proposal_capability_active=True)
    winner_token = next(item[1] for item in observed if item[0] == "ok")
    assert reader.show(proposal_id, 1).data["read_model_token"] == winner_token


@pytest.mark.parametrize("operation", ["decision", "withdraw"])
def test_service_real_lock_contention_deadline_has_no_late_admission(
        operation: str, tmp_path: Path) -> None:
    root = tmp_path / operation; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    capture, proposal_id, token = _base_for_mutation(root)
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    captured = {}
    original_begin = repository.begin_service_intent
    class CapturedIntent(Exception): pass
    def intercept(intent, precondition=None, deadline_monotonic=None):
        captured.update(intent=intent, precondition=precondition); raise CapturedIntent()
    repository.begin_service_intent = intercept
    try:
        with pytest.raises(CapturedIntent):
            if operation == "decision":
                service.decide(proposal_id, 1, "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
                    {"schema": 1, "decision": "accepted", "reason": "",
                     "previous_row_token": None, "read_model_token": token},
                    idempotency_key="N" * 32)
            else:
                service.withdraw(proposal_id, 1,
                    {"schema": 1, "read_model_token": token, "reason": "deadline"},
                    idempotency_key="N" * 32)
    finally:
        repository.begin_service_intent = original_begin

    context = multiprocessing.get_context("spawn"); ready = context.Event(); release = context.Event()
    holder = context.Process(target=_hold_repository_lock, args=(str(root), ready, release))
    holder.start(); assert ready.wait(10)
    started = time.monotonic()
    with pytest.raises(ProposalReviewError) as timed_out:
        service._admit_intent_with_deadline(captured["intent"], captured["precondition"],
                                            started + .05)
    assert timed_out.value.code == "service_mutation_in_progress"
    assert time.monotonic() - started < .2
    release.set(); holder.join(20); assert holder.exitcode == 0
    time.sleep(.1)
    observed, retained = repository.observe_service_intent_nonblocking(
        captured["intent"].scope, captured["intent"].key_digest,
        captured["intent"].request_fingerprint)
    assert observed and retained is None


@pytest.mark.parametrize("boundary", _METADATA_PHASES)
def test_service_04_keyless_decision(boundary: str, tmp_path: Path) -> None:
    root = tmp_path / boundary; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    capture, proposal_id, token = _base_for_mutation(root)
    process = multiprocessing.get_context("spawn").Process(
        target=_mutation_crash_worker, args=(str(root), boundary, "keyless", proposal_id, token))
    process.start(); process.join(20); assert process.exitcode == 78
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    assert service.show(proposal_id, 1).data["latest_decisions"]


@pytest.mark.parametrize("boundary", _METADATA_PHASES)
def test_service_03_keyed_decision_faults(boundary: str, tmp_path: Path) -> None:
    root = tmp_path / boundary; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    capture, proposal_id, token = _base_for_mutation(root)
    process = multiprocessing.get_context("spawn").Process(
        target=_mutation_crash_worker, args=(str(root), boundary, "decision", proposal_id, token))
    process.start(); process.join(20); assert process.exitcode == 78
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    replay = service.decide(proposal_id, 1, "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ",
        {"schema": 1, "decision": "accepted", "reason": "",
         "previous_row_token": None, "read_model_token": token},
        idempotency_key="K" * 32).data
    assert replay["decision"]["decision"] == "accepted"


@pytest.mark.parametrize("boundary", _METADATA_PHASES)
def test_service_05_withdraw(boundary: str, tmp_path: Path) -> None:
    root = tmp_path / boundary; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    capture, proposal_id, token = _base_for_mutation(root)
    process = multiprocessing.get_context("spawn").Process(
        target=_mutation_crash_worker, args=(str(root), boundary, "withdraw", proposal_id, token))
    process.start(); process.join(20); assert process.exitcode == 78
    repository = ProposalRepositoryV1.bootstrap(_adapter(root), clock=Clock(), entropy=SystemEntropy())
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    replay = service.withdraw(proposal_id, 1,
        {"schema": 1, "read_model_token": token, "reason": "stop"},
        idempotency_key="W" * 32).data
    assert replay["state"] == "withdrawn"
