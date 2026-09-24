from pathlib import Path

import pytest

from okf_loom.proposal_review.repository import ProposalRepositoryV1
from test_proposal_crash import _killed, _recover, _recover_existing


def test_wal_01_none(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "private_files_fsynced")
    publication.close()
    assert not any(item.phase != "pre_wal" for item in repository.recovery_report.items)
    assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()


def test_wal_02_prepared(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "wal_prepared")
    publication.close(); transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    wal = repository.read_wal(transaction_id)
    assert wal.phase == "prepared" and not wal.commit_decided and not any(wal.publish_bitmap)
    bundle, repository = _recover_existing(bundle, session)
    assert repository.read_wal(transaction_id).phase == "rolled_back"


def test_wal_03_commit_decided(tmp_path: Path) -> None:
    bundle, repository = _recover(tmp_path, "commit_decided")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_wal_04_publishing(tmp_path: Path) -> None:
    import multiprocessing
    from test_proposal_apply import _MultiCapture, _ready_two_target_transaction
    from test_proposal_crash import (_child_apply_two, DiskActivity, DiskEvents,
                                     _open)
    from okf_loom.proposal_review.publication import TrustedPublicationAdapter
    from okf_loom.proposal_review.transactions import ProposalTransactionCoordinator
    from test_proposal_service import Clock
    bundle, session, _, _, publication, request, _ = _ready_two_target_transaction(tmp_path)
    publication.close(); process = multiprocessing.get_context("fork").Process(
        target=_child_apply_two, args=(str(bundle), str(session), request, "publish_bitmap_advanced"))
    process.start(); process.join(30); assert process.exitcode == 77
    activity = DiskActivity(session); repository = _open(session, activity)
    transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    wal = repository.read_wal(transaction_id)
    assert wal.phase == "publishing" and wal.publish_bitmap == (True, False)
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    try:
        ProposalTransactionCoordinator(repository, _MultiCapture({name:
            f"---\ntype: Table\n---\n\n{name}\n".encode() for name in
            ("tables/customers", "tables/orders")}), publication, clock=Clock(),
            activity=activity, outbox=DiskEvents(session)).recover_all()
    finally:
        publication.close()
    assert repository.read_wal(transaction_id).phase == "committed"


def test_wal_05_content_published(tmp_path: Path) -> None:
    bundle, repository = _recover(tmp_path, "target_parent_fsynced")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_wal_06_finalizing(tmp_path: Path) -> None:
    bundle, repository = _recover(tmp_path, "audit_appended")
    audits = [row for row in repository.snapshot().aggregate("transactions")
              if row.kind == "transaction" and row.payload.get("operation") == "apply"]
    assert len(audits) == 1


def test_wal_07_committed_retirement(tmp_path: Path) -> None:
    from test_proposal_apply import _ready_transaction
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        result = coordinator.apply(request, "K" * 32)
        assert repository.can_retire_wal(result.transaction_id)
        repository.retire_wal(result.transaction_id)
        replay = coordinator.apply(request, "K" * 32)
        assert replay.transaction_id == result.transaction_id and replay.replayed
    finally: publication.close()


def test_wal_08_rollback(tmp_path: Path) -> None:
    bundle, repository = _recover(tmp_path, "wal_prepared")
    assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()


def test_wal_09_indeterminate(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    publication.close(); transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    stage = next((session / f"proposal-review-v1/transactions/{transaction_id}/stage").iterdir())
    stage.unlink(); stage.write_bytes(b"substituted"); stage.chmod(0o600)
    _, reopened = _recover_existing(bundle, session, indeterminate=True)
    assert reopened.read_wal(transaction_id).phase == "indeterminate"


def test_wal_validation_context_is_prepare_time_immutable() -> None:
    assert "validation_context_digest" in ProposalRepositoryV1._WAL_IMMUTABLE_FIELDS
    assert "transaction_intent_digest" in ProposalRepositoryV1._WAL_IMMUTABLE_FIELDS
