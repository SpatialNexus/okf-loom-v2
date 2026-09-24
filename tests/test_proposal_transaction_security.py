from pathlib import Path
import os

import pytest

from okf_loom.proposal_review.publication import TrustedPublicationAdapter
from okf_loom.proposal_review.transactions import ProposalTransactionCoordinator
from test_proposal_crash import _killed, DiskActivity, DiskEvents, _open
from test_proposal_service import Capture, Clock
from test_proposal_apply import _ready_transaction


@pytest.mark.parametrize("component", ["stage", "backup", "history", "intent", "canonical"])
def test_post_decision_missing_or_substituted_authority_is_indeterminate(tmp_path: Path, component: str) -> None:
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    publication.close(); transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    transaction = session / f"proposal-review-v1/transactions/{transaction_id}"
    if component in {"stage", "backup"}:
        path = next((transaction / component).iterdir()); path.unlink(); path.write_bytes(b"substituted"); path.chmod(0o600)
    elif component in {"history", "intent"}:
        (transaction / f"{component}.json").unlink()
    else:
        path = bundle / "tables/orders.md"; path.write_bytes(b"foreign canonical bytes"); path.chmod(0o600)
    activity = DiskActivity(session); reopened = _open(session, activity)
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    recovery = ProposalTransactionCoordinator(reopened, Capture(b"---\ntype: Table\n---\n\nOrders\n"),
        publication, clock=Clock(), activity=activity, outbox=DiskEvents(session))
    with pytest.raises(Exception) as blocked: recovery.recover_all()
    assert getattr(blocked.value, "code", None) == "transaction_recovering"
    assert reopened.read_wal(transaction_id).phase == "indeterminate"
    assert [row for row in reopened.snapshot().aggregate("corruption") if row.kind == "corruption"]
    publication.close()


def test_activity_substitution_during_finalization_preserves_evidence(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "audit_appended")
    publication.close(); transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    intent = repository.read_transaction_intent(transaction_id)
    activity_id = intent.value["candidate_ids"]["commit"]["activity_ids"][0]
    activity = DiskActivity(session)
    path = activity.root / f"{activity_id}.json"; path.write_text('{"wrong":true}'); path.chmod(0o600)
    reopened = _open(session, activity); publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    recovery = ProposalTransactionCoordinator(reopened, Capture(b"---\ntype: Table\n---\n\nOrders\n"),
        publication, clock=Clock(), activity=activity, outbox=DiskEvents(session))
    with pytest.raises(Exception): recovery.recover_all()
    assert path.read_text() == '{"wrong":true}'
    assert reopened.read_wal(transaction_id).phase == "indeterminate"
    publication.close()


@pytest.mark.parametrize("component", ["stage_missing", "stage_substituted",
    "backup_missing", "backup_substituted", "intent_missing", "intent_malformed",
    "intent_substituted", "history_missing", "history_malformed",
    "history_substituted", "current_resulting_substituted"])
def test_apply_11_undo_corruption_markers(tmp_path: Path, component: str) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    applied = coordinator.apply(request, "K" * 32)
    undo = {"schema": 1, "proposal_id": applied.proposal_id,
        "proposal_revision": applied.proposal_revision, "actor": "local",
        "transaction_id": applied.transaction_id, "undo_group_id": applied.undo_group_id,
        "read_model_token": request["read_model_token"]}
    def stop(boundary, evidence):
        if boundary == "commit_decided":
            raise RuntimeError("pause postdecision undo")
    coordinator.fault_injector = stop
    with pytest.raises(RuntimeError):
        coordinator.undo(undo, "U" * 32)
    undo_id = next(path.name for path in (session / "proposal-review-v1/transactions").iterdir()
                   if path.name.startswith("undo_"))
    transaction = session / f"proposal-review-v1/transactions/{undo_id}"
    visible = bundle / "tables/orders.md"
    expected_visible = visible.read_bytes()
    if component.startswith("stage") or component.startswith("backup"):
        directory = component.split("_", 1)[0]
        path = next((transaction / directory).iterdir())
        path.unlink()
        if component.endswith("substituted"):
            path.write_bytes(b"substituted"); path.chmod(0o600)
    elif component == "current_resulting_substituted":
        expected_visible = b"foreign current resulting authority"
        visible.write_bytes(expected_visible); visible.chmod(0o600)
    else:
        name = "intent.json" if component.startswith("intent") else "history.json"
        path = transaction / name
        if component.endswith("missing"):
            path.unlink()
        elif component.endswith("malformed"):
            path.write_bytes(b'{"malformed":true}'); path.chmod(0o600)
        else:
            parent = session / f"proposal-review-v1/transactions/{applied.transaction_id}/{name}"
            path.unlink(); path.write_bytes(parent.read_bytes()); path.chmod(0o600)
    publication.close(); disk_activity = DiskActivity(session); reopened = _open(session, disk_activity)
    recovered_publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    recovery = ProposalTransactionCoordinator(reopened,
        Capture(b"---\ntype: Table\n---\n\nOrders\n"), recovered_publication,
        clock=Clock(), activity=disk_activity, outbox=DiskEvents(session))
    try:
        with pytest.raises(Exception) as blocked:
            recovery.recover_all()
        assert getattr(blocked.value, "code", None) in {"transaction_recovering", "storage_corrupt"}
        assert reopened.read_wal(undo_id).phase == "indeterminate"
        markers = [row.payload for row in reopened.snapshot().aggregate("corruption")
                   if row.kind == "corruption"
                   and (row.payload.get("authority_identity") or {}).get("transaction_id") == undo_id]
        endpoints = [row.payload for row in reopened.snapshot().aggregate("endpoint_results")
                     if row.kind == "endpoint_result"
                     and row.payload.get("transaction_id") == undo_id
                     and row.payload.get("state") == "indeterminate"]
        assert len(markers) == 1 and len(endpoints) == 1
        assert not reopened.recovery_report.serving_safe
        assert visible.read_bytes() == expected_visible
    finally:
        recovered_publication.close()


@pytest.mark.parametrize("mutation", ["unknown_file", "unknown_link", "unknown_hard_link", "unknown_dir",
                                        "substituted_stage"])
def test_crash_17_unknown_prepare_cleanup(tmp_path: Path, mutation: str) -> None:
    bundle, session, repository, coordinator, publication, request = _killed(
        tmp_path, "intent_published")
    transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    transaction = session / f"proposal-review-v1/transactions/{transaction_id}"
    if mutation == "unknown_file":
        path = transaction / "unknown.bin"; path.write_bytes(b"evidence"); path.chmod(0o600)
    elif mutation == "unknown_link":
        (transaction / "unknown.link").symlink_to("intent.json")
    elif mutation == "unknown_hard_link":
        os.link(next((transaction / "stage").iterdir()), transaction / "unknown.hard")
    elif mutation == "unknown_dir":
        extra = transaction / "unknown"; extra.mkdir(mode=0o700)
        evidence = extra / "evidence"; evidence.write_bytes(b"x"); evidence.chmod(0o600)
    else:
        stage = next((transaction / "stage").iterdir())
        replacement = stage.with_suffix(".new"); replacement.write_bytes(stage.read_bytes())
        replacement.chmod(0o600); replacement.replace(stage)
    with pytest.raises(Exception) as blocked:
        coordinator.recover_all()
    assert getattr(blocked.value, "code", None) in {"storage_corrupt", "storage_unsafe"}
    assert transaction.exists() and any(transaction.rglob("*"))
    publication.close()


@pytest.mark.parametrize("boundary", ["after_rename_before_source_verify", "parent_fsynced"])
@pytest.mark.parametrize("authority", ["source", "destination", "parent"])
def test_sec_11_after_syscall_identity_seams(tmp_path: Path, boundary: str,
                                             authority: str) -> None:
    """Hostile post-syscall substitution is durable indeterminate authority."""
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    preserved: list[Path] = []

    def substitute(observed: str, evidence: dict[str, object]) -> None:
        if observed != boundary:
            return
        transaction = session / "proposal-review-v1/transactions" / str(evidence["transaction_id"])
        if authority == "source":
            source = next((transaction / "stage").iterdir())
            saved = source.with_name(source.name + ".sec11-evidence")
            source.rename(saved)
            source.write_bytes(b"hostile source substitution")
            source.chmod(0o600)
        elif authority == "destination":
            destination = bundle / "tables/orders.md"
            saved = destination.with_name(destination.name + ".sec11-evidence")
            destination.rename(saved)
            destination.write_bytes(b"hostile destination substitution")
            destination.chmod(0o600)
        else:
            parent = bundle / "tables"
            saved = bundle / "tables.sec11-evidence"
            parent.rename(saved)
            parent.mkdir(mode=0o755)
            destination = parent / "orders.md"
            destination.write_bytes(b"hostile parent substitution")
            destination.chmod(0o600)
        preserved.append(saved)

    publication._fault_injector = substitute
    try:
        with pytest.raises(Exception) as failed:
            coordinator.apply(request, "S" * 32)
        assert getattr(failed.value, "code", None) == "indeterminate_outcome"
    finally:
        publication.close()

    assert len(preserved) == 1 and preserved[0].exists()
    reopened = _open(session, DiskActivity(session))
    transaction_id = next(item.transaction_id for item in reopened.recovery_report.items)
    wal = reopened.read_wal(transaction_id)
    assert wal.phase == "indeterminate"
    markers = [row.payload for row in reopened.snapshot().aggregate("corruption")
               if row.kind == "corruption"
               and (row.payload.get("authority_identity") or {}).get("transaction_id") == transaction_id]
    endpoints = [row.payload for row in reopened.snapshot().aggregate("endpoint_results")
                 if row.kind == "endpoint_result" and row.payload.get("transaction_id") == transaction_id]
    assert len(markers) == 1
    assert len([row for row in endpoints if row.get("state") == "indeterminate"]) == 1
    assert not [row for row in endpoints if row.get("state") == "terminal"]
    assert not reopened.recovery_report.serving_safe
    assert (session / "proposal-review-v1/transactions" / transaction_id).exists()
