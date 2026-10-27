from pathlib import Path

import pytest

from okf_loom.proposal_review.errors import RepositoryError, StorageSafetyError
from okf_loom.proposal_review.ports import LinuxDirFdFilesystem
from okf_loom.proposal_review.repository import ProposalRepositoryV1
from okf_loom.proposal_review.publication import TrustedPublicationAdapter
from okf_loom.proposal_review.transactions import TransactionTerminalError


def _swap_at_exchange_seam(action: str, bundle: str, repository: str,
                           transaction_id: str, start, done) -> None:
    import os
    bundle_path, repository_path = Path(bundle), Path(repository)
    start.wait(10)
    canonical = bundle_path / "tables/orders.md"
    stage = repository_path / "transactions" / transaction_id / "stage/000000.bin"
    if action == "canonical_same_inode_swap":
        replacement = canonical.with_suffix(".new"); replacement.write_bytes(b"before\n")
        replacement.chmod(0o600); replacement.replace(canonical)
    elif action == "stage_same_inode_swap":
        replacement = stage.with_suffix(".new"); replacement.write_bytes(b"after\n")
        replacement.chmod(0o600); replacement.replace(stage)
    elif action == "canonical_hardlink":
        source = bundle_path / "hardlink-source"; source.write_bytes(b"before\n"); source.chmod(0o600)
        canonical.unlink(); os.link(source, canonical)
    elif action == "stage_symlink":
        source = stage.with_suffix(".source"); source.write_bytes(b"after\n"); source.chmod(0o600)
        stage.unlink(); stage.symlink_to(source.name)
    elif action == "nested_parent":
        parent = bundle_path / "tables"; old = bundle_path / "tables-old"
        parent.rename(old); parent.mkdir(mode=0o700)
        replacement = parent / "orders.md"; replacement.write_bytes(b"before\n"); replacement.chmod(0o600)
    done.set()


def _paused_pre_wal_creator(bundle: str, session: str, ready, release) -> None:
    from test_proposal_crash import DiskActivity, _open
    bundle_path, session_path = Path(bundle), Path(session)
    repository = _open(session_path, DiskActivity(session_path))
    publication = TrustedPublicationAdapter(bundle_path, session_path / "proposal-review-v1")
    transaction_id = "txn_" + "7" * 26
    lineage = "creator:1"
    with repository.transaction_session() as locks:
        with locks.ranked("bundle"):
            with publication.exclusive():
                with locks.ranked("concept"):
                    with publication.concept_locks(["tables/orders"]):
                        with locks.locked("lineage", lineage):
                            with locks.locked("endpoint", lineage):
                                with locks.locked("transaction", transaction_id):
                                    with locks.locked("history", transaction_id):
                                        with locks.locked("activity_outbox", lineage):
                                            publication.prepare(transaction_id, {
                                                "tables/orders": b"after\n"})
                                            ready.set(); release.wait(20)
    publication.close()


def _startup_recover_process(bundle: str, session: str, done) -> None:
    from test_proposal_crash import DiskActivity, DiskEvents, _open
    from test_proposal_service import Capture, Clock
    from okf_loom.proposal_review.transactions import ProposalTransactionCoordinator
    bundle_path, session_path = Path(bundle), Path(session)
    activity = DiskActivity(session_path); repository = _open(session_path, activity)
    publication = TrustedPublicationAdapter(bundle_path, session_path / "proposal-review-v1")
    ProposalTransactionCoordinator(repository,
        Capture(b"---\ntype: Table\n---\n\nOrders\n"), publication, clock=Clock(),
        activity=activity, outbox=DiskEvents(session_path)).recover_all()
    publication.close(); done.set()


def test_state_18_waiters_are_not_persisted(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    fd = __import__("os").open(tmp_path, __import__("os").O_RDONLY | __import__("os").O_DIRECTORY)
    fs = LinuxDirFdFilesystem.adopt(fd)
    repo = ProposalRepositoryV1.bootstrap(fs)
    with repo.transaction_session() as session:
        with session.ranked("bundle"):
            pass
        with pytest.raises(RepositoryError):
            with session.ranked("bundle"):
                pass


def test_state_18_waiters(tmp_path: Path) -> None:
    import time
    from test_proposal_apply import _ready_transaction
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    delayed = False
    def fault(boundary, evidence):
        nonlocal delayed
        if boundary == "commit_decided" and not delayed:
            delayed = True; time.sleep(1.0)
    coordinator.fault_injector = fault; coordinator.wait_seconds = .7
    try:
        progress = coordinator.apply(request, "K" * 32)
        assert progress.commit_decided and coordinator._waiters
        until = time.monotonic() + 5
        while coordinator._waiters and time.monotonic() < until: time.sleep(.02)
        assert not coordinator._waiters
        reopened = __import__("test_proposal_crash")._open(session, activity)
        assert not hasattr(reopened, "_waiters")
    finally: publication.close()


def test_state_06_wal_result_authority(tmp_path: Path) -> None:
    from test_proposal_crash import _killed
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    try:
        transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
        assert repository.read_wal(transaction_id).phase == "commit_decided"
        results = [row.payload for row in repository.snapshot().aggregate("endpoint_results")
                   if row.kind == "endpoint_result" and row.payload.get("transaction_id") == transaction_id]
        assert [row["state"] for row in results] == ["prepared"]
        coordinator.recover_all()
        assert repository.read_wal(transaction_id).phase == "committed"
        assert [row.payload["state"] for row in repository.snapshot().aggregate("endpoint_results")
                if row.kind == "endpoint_result" and row.payload.get("transaction_id") == transaction_id][-1] == "terminal"
    finally: publication.close()


def test_recovery_consumes_terminal_rollback_but_observe_preserves_error(tmp_path: Path) -> None:
    from test_proposal_crash import _killed

    bundle, session, repository, coordinator, publication, request = _killed(
        tmp_path, "wal_prepared")
    try:
        transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
        assert transaction_id in coordinator.recover_all()
        assert repository.read_wal(transaction_id).phase == "rolled_back"
        with pytest.raises(TransactionTerminalError) as terminal:
            coordinator.observe(transaction_id)
        assert terminal.value.code == "apply_rejected"
    finally:
        publication.close()


def test_publication_preserves_mode_and_exact_bytes(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"; repository = tmp_path / "repository"
    bundle.mkdir(mode=0o700); repository.mkdir(mode=0o700)
    bundle.chmod(0o700); repository.chmod(0o700)
    concept = bundle / "orders.md"; concept.write_bytes(b"before\n"); concept.chmod(0o640)
    transaction_id = "txn_" + "0" * 26
    with TrustedPublicationAdapter(bundle, repository) as publication:
        prepared = publication.prepare(transaction_id, {"orders": b"after\n"})
        assert publication.classify(prepared.targets[0]) == "before"
        publication.publish(transaction_id, prepared.targets[0])
        assert publication.classify(prepared.targets[0]) == "staged"
    assert concept.read_bytes() == b"after\n"
    assert concept.stat().st_mode & 0o777 == 0o640


def _publication_fixture(tmp_path: Path, *, nested: bool = False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bundle = tmp_path / "bundle"; repository = tmp_path / "repository"
    bundle.mkdir(mode=0o700); repository.mkdir(mode=0o700)
    bundle.chmod(0o700); repository.chmod(0o700)
    parent = bundle / "tables" if nested else bundle
    if nested: parent.mkdir(mode=0o700)
    concept = parent / "orders.md"; concept.write_bytes(b"before\n"); concept.chmod(0o600)
    return bundle, repository, concept, "tables/orders" if nested else "orders"


def test_publication_rejects_same_byte_before_inode_substitution(tmp_path: Path) -> None:
    bundle, repository, concept, concept_id = _publication_fixture(tmp_path)
    with TrustedPublicationAdapter(bundle, repository) as publication:
        prepared = publication.prepare("txn_" + "1" * 26, {concept_id: b"after\n"})
        replacement = concept.with_suffix(".new"); replacement.write_bytes(b"before\n")
        replacement.chmod(0o600); replacement.replace(concept)
        with pytest.raises(StorageSafetyError):
            publication.publish(prepared.transaction_id, prepared.targets[0])


def test_publication_rejects_stage_substitution_and_links(tmp_path: Path) -> None:
    import os
    bundle, repository, concept, concept_id = _publication_fixture(tmp_path)
    transaction_id = "txn_" + "2" * 26
    with TrustedPublicationAdapter(bundle, repository) as publication:
        prepared = publication.prepare(transaction_id, {concept_id: b"after\n"})
        stage = repository / "transactions" / transaction_id / "stage" / "000000.bin"
        substitute = stage.with_suffix(".new"); substitute.write_bytes(b"after\n")
        substitute.chmod(0o600); substitute.replace(stage)
        with pytest.raises(StorageSafetyError):
            publication.publish(transaction_id, prepared.targets[0])
    # Canonical hardlinks and symlinks are rejected before any rename.
    concept.unlink(); source = bundle / "source.md"; source.write_bytes(b"before\n")
    os.link(source, concept)
    with TrustedPublicationAdapter(bundle, repository) as publication:
        with pytest.raises(StorageSafetyError):
            publication.canonical_identity(concept_id)
    concept.unlink(); concept.symlink_to(source)
    with TrustedPublicationAdapter(bundle, repository) as publication:
        with pytest.raises(OSError):
            publication.canonical_identity(concept_id)


def test_publication_rejects_nested_parent_and_post_rename_substitution(tmp_path: Path) -> None:
    import dataclasses
    bundle, repository, concept, concept_id = _publication_fixture(tmp_path, nested=True)
    transaction_id = "txn_" + "3" * 26
    with TrustedPublicationAdapter(bundle, repository) as publication:
        prepared = publication.prepare(transaction_id, {concept_id: b"after\n"})
        old_parent = bundle / "tables-old"
        (bundle / "tables").rename(old_parent); (bundle / "tables").mkdir(mode=0o700)
        new_concept = bundle / "tables" / "orders.md"; new_concept.write_bytes(b"before\n")
        new_concept.chmod(0o600)
        with pytest.raises(StorageSafetyError):
            publication.publish(transaction_id, prepared.targets[0])
    # A same-byte replacement after a completed rename is not concurrent completion.
    bundle2, repository2, concept2, concept_id2 = _publication_fixture(tmp_path / "second")
    with TrustedPublicationAdapter(bundle2, repository2) as publication:
        prepared = publication.prepare("txn_" + "4" * 26, {concept_id2: b"after\n"})
        resulting = publication.publish(prepared.transaction_id, prepared.targets[0])
        bound = dataclasses.replace(prepared.targets[0], resulting_identity=resulting)
        replacement = concept2.with_suffix(".new"); replacement.write_bytes(b"after\n")
        replacement.chmod(0o600); replacement.replace(concept2)
        with pytest.raises(StorageSafetyError):
            publication.publish(prepared.transaction_id, bound)


@pytest.mark.parametrize("action", ["canonical_same_inode_swap", "stage_same_inode_swap",
    "canonical_hardlink", "stage_symlink", "nested_parent"])
@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_publication_process_substitution_at_check_exchange_seam(tmp_path: Path,
                                                                 action: str) -> None:
    import multiprocessing
    bundle, repository, _concept, concept_id = _publication_fixture(tmp_path, nested=True)
    transaction_id = "txn_" + str(len(action)).zfill(26)
    context = multiprocessing.get_context("fork")
    start, done = context.Event(), context.Event()
    worker = context.Process(target=_swap_at_exchange_seam,
        args=(action, str(bundle), str(repository), transaction_id, start, done))
    def synchronize(boundary, evidence):
        if boundary == "before_rename":
            start.set(); assert done.wait(10)
    with TrustedPublicationAdapter(bundle, repository, fault_injector=synchronize) as publication:
        prepared = publication.prepare(transaction_id, {concept_id: b"after\n"})
        worker.start()
        with pytest.raises((StorageSafetyError, OSError)):
            publication.publish(transaction_id, prepared.targets[0])
    worker.join(15)
    assert worker.exitcode == 0


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_recovery_does_not_clean_paused_pre_wal_creator(tmp_path: Path) -> None:
    import multiprocessing
    from test_proposal_apply import _ready_transaction
    from test_proposal_crash import DiskActivity, _open
    bundle, session, _repository, _coordinator, publication, _request, _activity = _ready_transaction(tmp_path)
    publication.close(); context = multiprocessing.get_context("fork")
    ready, release, done = context.Event(), context.Event(), context.Event()
    creator = context.Process(target=_paused_pre_wal_creator,
                              args=(str(bundle), str(session), ready, release))
    creator.start(); assert ready.wait(15)
    recovery = context.Process(target=_startup_recover_process,
                               args=(str(bundle), str(session), done))
    recovery.start()
    assert not done.wait(1), "startup cleanup raced an active pre-WAL creator"
    release.set(); creator.join(20); recovery.join(30)
    assert creator.exitcode == 0 and recovery.exitcode == 0 and done.is_set()
    reopened = _open(session, DiskActivity(session))
    assert not reopened.recovery_report.items


def test_publication_has_no_rename_fallback_when_exchange_unavailable(tmp_path: Path,
                                                                      monkeypatch) -> None:
    from okf_loom.proposal_review import publication as publication_module
    bundle, repository, concept, concept_id = _publication_fixture(tmp_path, nested=True)
    with TrustedPublicationAdapter(bundle, repository) as publication:
        prepared = publication.prepare("txn_" + "8" * 26, {concept_id: b"after\n"})
        def unavailable(*args, **kwargs):
            raise StorageSafetyError("renameat2 unavailable")
        monkeypatch.setattr(publication_module, "_rename_exchange", unavailable)
        with pytest.raises(StorageSafetyError):
            publication.publish(prepared.transaction_id, prepared.targets[0])
        assert concept.read_bytes() == b"before\n"
        stage = repository / "transactions" / prepared.transaction_id / "stage/000000.bin"
        assert stage.read_bytes() == b"after\n"
