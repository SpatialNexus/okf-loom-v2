"""RET-SVC-01..03 direct executable retention proofs."""
from __future__ import annotations

import dataclasses
import multiprocessing
import os

import pytest

from okf_loom.proposal_review.errors import RepositoryQuotaError
from okf_loom.proposal_review.ports import RuntimeIdentities, SystemEntropy
from okf_loom.proposal_review.repository import ProposalRepositoryV1, RepositoryLimits
from okf_loom.proposal_review.service import ProposalService
from okf_loom.proposal_review import schemas
from test_proposal_service import Capture, RUNTIME
from test_proposal_service_recovery import _adapter, _crash_worker, _request

TS = "2026-07-14T12:34:56.789Z"


class MutableClock:
    def __init__(self): self.value = TS
    def now(self): return self.value


def _created(root, *, limits=None):
    clock = MutableClock(); capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    repository = ProposalRepositoryV1.bootstrap(
        _adapter(root), clock=clock, entropy=SystemEntropy(), limits=limits)
    service = ProposalService(repository, capture, clock,
        RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    value = service.create(_request(capture), idempotency_key="T" * 32).data
    return clock, repository, service, value


def test_ret_svc_01_result_horizon(tmp_path):
    root = tmp_path / "result"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    clock, repository, _, value = _created(root)
    intent = repository.snapshot().service_intents[0]
    clock.value = "2026-07-21T12:34:56.789Z"
    compacted = repository.reconcile_endpoint(intent.scope, intent.key_digest,
                                              intent.request_fingerprint)
    assert compacted.state == "terminal_compacted"
    assert compacted.tombstone.result_record_id == intent.assigned["result_record_id"]
    proposal = value["proposal"]
    assert repository.get_preview(proposal["preview_ref"]["preview_digest"]).preview_digest == \
        proposal["preview_ref"]["preview_digest"]


def test_ret_svc_02_tombstone_horizon(tmp_path):
    root = tmp_path / "audit"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    clock, repository, _, _ = _created(root); intent = repository.snapshot().service_intents[0]
    clock.value = "2026-07-21T12:34:56.789Z"
    repository.reconcile_endpoint(intent.scope, intent.key_digest, intent.request_fingerprint)
    clock.value = "2027-01-10T12:34:56.789Z"  # exact 180-day horizon
    assert repository.prune_service_retention() == 0
    assert repository.snapshot().service_intents[0].intent_id == intent.intent_id
    assert any(row.kind == "tombstone" for row in repository.snapshot().aggregate("tombstones"))
    clock.value = "2027-01-10T12:34:56.790Z"
    assert repository.prune_service_retention() == 1
    assert repository.reconcile_endpoint(intent.scope, intent.key_digest,
                                         intent.request_fingerprint).state == "new"

    uncertain = tmp_path / "indeterminate"; uncertain.mkdir(mode=0o700); os.chmod(uncertain, 0o700)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_worker, args=(str(uncertain), "service_intent_prepared"))
    process.start(); process.join(20); assert process.exitcode == 77
    repo_root = _adapter(uncertain).subroot("proposal-review-v1")
    staged_id = repo_root.list_dir("preview-staging")[0]
    repo_root.unlink(f"preview-staging/{staged_id}/preview.json")
    for _ in range(100):
        ProposalRepositoryV1.bootstrap(_adapter(uncertain), clock=MutableClock(),
                                       entropy=SystemEntropy())
    uncertain_clock = MutableClock()
    uncertain_repo = ProposalRepositoryV1.bootstrap(
        _adapter(uncertain), clock=uncertain_clock, entropy=SystemEntropy())
    uncertain_intent = uncertain_repo.snapshot().service_intents[0]
    assert uncertain_intent.phase == "indeterminate"
    uncertain_clock.value = "2026-07-21T12:34:56.789Z"
    uncertain_repo.reconcile_endpoint(uncertain_intent.scope, uncertain_intent.key_digest,
                                      uncertain_intent.request_fingerprint)
    uncertain_clock.value = "2027-01-10T12:34:56.789Z"
    assert uncertain_repo.prune_service_retention() == 0
    assert uncertain_repo.snapshot().service_intents[0].phase == "indeterminate"
    assert any(row.kind == "tombstone" for row in
               uncertain_repo.snapshot().aggregate("tombstones"))


def test_ret_svc_03_wal_pruning(tmp_path):
    nonterminal = tmp_path / "nonterminal"; nonterminal.mkdir(mode=0o700); os.chmod(nonterminal, 0o700)
    clock = MutableClock(); capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    class Injected(Exception): pass
    def fault(boundary):
        if boundary == "service_intent_prepared": raise Injected()
    repository = ProposalRepositoryV1.bootstrap(
        _adapter(nonterminal), clock=clock, entropy=SystemEntropy(), fault_injector=fault)
    service = ProposalService(repository, capture, clock,
        RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME), proposal_capability_active=True)
    with pytest.raises(Injected):
        service.create(_request(capture), idempotency_key="P" * 32)
    prepared = repository.snapshot().service_intents[0]
    assert prepared.phase == "prepared"
    assert repository._fs.exists(prepared.preview["staged_ref"]["relative_path"])
    clock.value = "2027-01-10T12:34:56.790Z"
    assert repository.prune_service_retention() == 0
    assert repository.snapshot().service_intents[0].phase == "prepared"
    assert repository._fs.exists(prepared.preview["staged_ref"]["relative_path"])

    uncertain = tmp_path / "uncertain-prune"; uncertain.mkdir(mode=0o700); os.chmod(uncertain, 0o700)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_worker, args=(str(uncertain), "service_intent_prepared"))
    process.start(); process.join(20); assert process.exitcode == 77
    repo_root = _adapter(uncertain).subroot("proposal-review-v1")
    staged_id = repo_root.list_dir("preview-staging")[0]
    repo_root.unlink(f"preview-staging/{staged_id}/preview.json")
    for _ in range(100):
        ProposalRepositoryV1.bootstrap(_adapter(uncertain), clock=MutableClock(),
                                       entropy=SystemEntropy())
    uncertain_clock = MutableClock(); uncertain_clock.value = "2027-01-10T12:34:56.790Z"
    uncertain_repo = ProposalRepositoryV1.bootstrap(
        _adapter(uncertain), clock=uncertain_clock, entropy=SystemEntropy())
    assert uncertain_repo.prune_service_retention() == 0
    assert uncertain_repo.snapshot().service_intents[0].phase == "indeterminate"

    eligible = tmp_path / "eligible"; eligible.mkdir(mode=0o700); os.chmod(eligible, 0o700)
    eligible_clock, eligible_repo, _, _ = _created(eligible)
    eligible_clock.value = "2027-01-10T12:34:56.790Z"
    assert eligible_repo.prune_service_retention() == 1
    assert eligible_repo.snapshot().service_intents == ()

    retained = tmp_path / "retained"; retained.mkdir(mode=0o700); os.chmod(retained, 0o700)
    limits = dataclasses.replace(RepositoryLimits(), service_wal_retained_files=1)
    clock, repository, service, _ = _created(retained, limits=limits)
    capture = service.capture_port
    with pytest.raises(RepositoryQuotaError):
        service.create(_request(capture), idempotency_key="U" * 32)
    assert len(repository.snapshot().service_intents) == 1
