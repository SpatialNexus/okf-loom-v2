import pytest
from pathlib import Path
import multiprocessing

from okf_loom.proposal_review.errors import SchemaError
from okf_loom.proposal_review.service_contracts import UndoInput
from test_proposal_apply import _ready_transaction, _ready_two_target_transaction
from test_proposal_crash import DiskActivity, DiskEvents, _open
from test_proposal_service import Capture, Clock
from okf_loom.proposal_review.publication import TrustedPublicationAdapter
from okf_loom.proposal_review.transactions import ProposalTransactionCoordinator

D = "sha256:" + "0" * 64
U = "0" * 26


def test_undo_input_is_closed_and_proposal_linked() -> None:
    value = {"schema": 1, "proposal_id": U, "proposal_revision": 1, "actor": "local",
             "transaction_id": "txn_" + U, "undo_group_id": "undo_" + U,
             "read_model_token": D}
    assert UndoInput.from_mapping(value).transaction_id.startswith("txn_")
    with pytest.raises(SchemaError):
        UndoInput.from_mapping({**value, "targets": []})


def test_state_09_undo_authority(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        applied = coordinator.apply(request, "K" * 32)
        audit = next(__import__("okf_loom.proposal_review.schemas", fromlist=["TransactionAudit"]).TransactionAudit.from_mapping(row.payload)
                     for row in repository.snapshot().aggregate("transactions")
                     if row.kind == "transaction" and row.payload.get("transaction_id") == applied.transaction_id)
        intent, history = repository.verify_audit_transaction_chain(audit)
        assert intent.value["history"]["manifest_digest"] == history.canonical_digest()
        assert audit.history_manifest_digest == history.canonical_digest()
        assert all("resulting_identity" not in target.to_mapping() for target in history.targets)
        assert history.undo_group_id == applied.undo_group_id
    finally: publication.close()


def test_apply_09_undo_success(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        applied = coordinator.apply(request, "K" * 32)
        _, endpoint_faults = repository._scan_aggregate_locked("endpoint_results")
        assert not endpoint_faults, endpoint_faults
        assert not [row.payload for row in repository.snapshot().aggregate("corruption")
                    if row.kind == "corruption"], [row.payload for row in repository.snapshot().aggregate("corruption")]
        undone = coordinator.undo({"schema": 1, "proposal_id": applied.proposal_id,
            "proposal_revision": applied.proposal_revision, "actor": "local",
            "transaction_id": applied.transaction_id, "undo_group_id": applied.undo_group_id,
            "read_model_token": request["read_model_token"]}, "U" * 32)
    finally: publication.close()
    assert undone.status == "undone"
    assert (bundle / "tables/orders.md").read_bytes() == b"---\ntype: Table\n---\n\nOrders\n"
    assert repository.read_wal(undone.transaction_id).phase == "committed"


def test_apply_10_undo_blocked(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_two_target_transaction(tmp_path)
    try:
        applied = coordinator.apply(request, "K" * 32)
        path = bundle / "tables/customers.md"; path.write_bytes(path.read_bytes() + b"foreign\n")
        other = bundle / "tables/orders.md"; applied_other = other.read_bytes()
        with pytest.raises(Exception) as blocked:
            coordinator.undo({"schema": 1, "proposal_id": applied.proposal_id,
                "proposal_revision": applied.proposal_revision, "actor": "local",
                "transaction_id": applied.transaction_id, "undo_group_id": applied.undo_group_id,
                "read_model_token": request["read_model_token"]}, "U" * 32)
        assert getattr(blocked.value, "code", None) == "proposal_conflict"
        assert path.read_bytes().endswith(b"foreign\n")
        assert other.read_bytes() == applied_other and b"tags:" in applied_other
    finally: publication.close()


def test_state_09_undo_blocks_change_away_back_aba(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        applied = coordinator.apply(request, "K" * 32)
        path = bundle / "tables/orders.md"; exact_applied = path.read_bytes()
        path.write_bytes(b"foreign\n")
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(exact_applied); replacement.chmod(0o600); replacement.replace(path)
        with pytest.raises(Exception) as blocked:
            coordinator.undo({"schema": 1, "proposal_id": applied.proposal_id,
                "proposal_revision": applied.proposal_revision, "actor": "local",
                "transaction_id": applied.transaction_id, "undo_group_id": applied.undo_group_id,
                "read_model_token": request["read_model_token"]}, "U" * 32)
        assert getattr(blocked.value, "code", None) == "proposal_conflict"
        assert path.read_bytes() == exact_applied
    finally:
        publication.close()


def test_async_09_undo_mid_publish_recovers_exactly(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    applied = coordinator.apply(request, "K" * 32)
    undo = {"schema": 1, "proposal_id": applied.proposal_id,
        "proposal_revision": applied.proposal_revision, "actor": "local",
        "transaction_id": applied.transaction_id, "undo_group_id": applied.undo_group_id,
        "read_model_token": request["read_model_token"]}
    tripped = False
    def crash(boundary, evidence):
        nonlocal tripped
        if boundary == "target_parent_fsynced" and not tripped:
            tripped = True
            raise RuntimeError("simulated undo process loss after canonical rename")
    coordinator.fault_injector = crash
    with pytest.raises(RuntimeError):
        coordinator.undo(undo, "U" * 32)
    publication.close()
    disk_activity = DiskActivity(session); reopened = _open(session, disk_activity)
    recovered_publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    recovery = ProposalTransactionCoordinator(reopened,
        Capture(b"---\ntype: Table\n---\n\nOrders\n"), recovered_publication,
        clock=Clock(), activity=disk_activity, outbox=DiskEvents(session))
    try:
        recovery.recover_all()
        undo_wals = [item for item in reopened.recovery_report.items
                     if item.transaction_id.startswith("undo_")]
        assert undo_wals and all(item.classification == "terminal" for item in undo_wals)
        assert (bundle / "tables/orders.md").read_bytes() == b"---\ntype: Table\n---\n\nOrders\n"
    finally:
        recovered_publication.close()


def _child_undo(bundle: str, session: str, request: dict, key: str) -> None:
    import os
    bundle_path, session_path = Path(bundle), Path(session); activity = DiskActivity(session_path)
    repository = _open(session_path, activity)
    publication = TrustedPublicationAdapter(bundle_path, session_path / "proposal-review-v1")
    coordinator = ProposalTransactionCoordinator(repository,
        Capture(b"---\ntype: Table\n---\n\nOrders\n"), publication, clock=Clock(),
        activity=activity, outbox=DiskEvents(session_path))
    try: coordinator.undo(request, key)
    except Exception: os._exit(78)
    os._exit(0)


def test_undo_cross_process_same_key_race(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    applied = coordinator.apply(request, "K" * 32); publication.close()
    undo = {"schema": 1, "proposal_id": applied.proposal_id, "proposal_revision": 1,
        "actor": "local", "transaction_id": applied.transaction_id,
        "undo_group_id": applied.undo_group_id, "read_model_token": request["read_model_token"]}
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_child_undo, args=(str(bundle), str(session), undo, "U" * 32))
               for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(30)
    assert [worker.exitcode for worker in workers] == [0, 0]
    reopened = _open(session, DiskActivity(session))
    assert len([row for row in reopened.snapshot().aggregate("transactions")
                if row.kind == "transaction" and row.payload.get("operation") == "undo"]) == 1


def test_undo_cross_process_different_key_race(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    applied = coordinator.apply(request, "K" * 32); publication.close()
    undo = {"schema": 1, "proposal_id": applied.proposal_id, "proposal_revision": 1,
        "actor": "local", "transaction_id": applied.transaction_id,
        "undo_group_id": applied.undo_group_id, "read_model_token": request["read_model_token"]}
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_child_undo, args=(str(bundle), str(session), undo, char * 32))
               for char in ("U", "V")]
    for worker in workers: worker.start()
    for worker in workers: worker.join(30)
    assert sorted(worker.exitcode for worker in workers) == [0, 78]
