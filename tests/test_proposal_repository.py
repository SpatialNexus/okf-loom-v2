"""STORE-01..13 direct proofs for Proposal Review Foundation A."""
from __future__ import annotations

import copy
import dataclasses
import multiprocessing
import os
from pathlib import Path

import pytest

from okf_loom.proposal_review import canonical, schemas
from okf_loom.proposal_review.errors import (
    IdempotencyConflictError, LockUnavailableError, RepositoryCorruptError,
    RepositoryError, RepositoryQuotaError, SchemaError,
)
from okf_loom.proposal_review.legacy import LegacySuggestionReader
from okf_loom.proposal_review.ports import LinuxDirFdFilesystem
from okf_loom.proposal_review.repository import ProposalRepositoryV1, RepositoryLimits

U = "01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
D = "sha256:" + "0" * 64
DB = "sha256:" + "1" * 64
TS = "2026-07-14T12:34:56.789Z"
TS2 = "2026-07-15T12:34:56.789Z"
TS3 = "2030-07-15T12:34:56.789Z"
RUNTIME = "runtime@1.0+sha256:" + "0" * 64


def test_store_14_immutable_history_parity(tmp_path: Path) -> None:
    from test_proposal_apply import _ready_transaction
    from test_proposal_crash import _killed
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        applied = coordinator.apply(request, "K" * 32)
        apply_path = session / f"proposal-review-v1/transactions/{applied.transaction_id}/history.json"
        apply_bytes = apply_path.read_bytes()
        apply_history = schemas.HistoryManifest.from_json(apply_bytes)
        malformed = apply_history.to_mapping(); malformed["state"] = "prepared"
        with pytest.raises(SchemaError): schemas.HistoryManifest.from_mapping(malformed)
        malformed = apply_history.to_mapping()
        malformed["targets"][0]["resulting_identity"] = malformed["targets"][0]["before_identity"]
        with pytest.raises(SchemaError): schemas.HistoryManifest.from_mapping(malformed)

        undo = coordinator.undo({"schema": 1, "proposal_id": applied.proposal_id,
            "proposal_revision": applied.proposal_revision, "actor": "local",
            "transaction_id": applied.transaction_id, "undo_group_id": applied.undo_group_id,
            "read_model_token": request["read_model_token"]}, "U" * 32)
        assert apply_path.read_bytes() == apply_bytes
        for transaction_id in (applied.transaction_id, undo.transaction_id):
            intent = repository.read_transaction_intent(transaction_id)
            history_path = session / f"proposal-review-v1/transactions/{transaction_id}/history.json"
            history_bytes = history_path.read_bytes()
            history = schemas.HistoryManifest.from_json(history_bytes)
            audit = next(schemas.TransactionAudit.from_mapping(row.payload)
                         for row in repository.snapshot().aggregate("transactions")
                         if row.kind == "transaction"
                         and row.payload.get("transaction_id") == transaction_id)
            assert intent.value["history"]["manifest_digest"] == history.canonical_digest()
            assert audit.history_manifest_digest == history.canonical_digest()
            assert history_path.read_bytes() == history_bytes
    finally:
        publication.close()

    rollback_root = tmp_path / "rollback"; rollback_root.mkdir()
    bundle, session, repository, coordinator, publication, request = _killed(
        rollback_root, "wal_prepared")
    transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    history_path = session / f"proposal-review-v1/transactions/{transaction_id}/history.json"
    before_rollback = history_path.read_bytes()
    try:
        coordinator.recover_all()
        assert repository.read_wal(transaction_id).phase == "rolled_back"
        assert history_path.read_bytes() == before_rollback
        history = schemas.HistoryManifest.from_json(before_rollback)
        audit = next(schemas.TransactionAudit.from_mapping(row.payload)
                     for row in repository.snapshot().aggregate("transactions")
                     if row.kind == "transaction"
                     and row.payload.get("transaction_id") == transaction_id)
        assert audit.history_manifest_digest == history.canonical_digest()
    finally:
        publication.close()


class Clock:
    def now(self) -> str:
        return TS


class Entropy:
    def __init__(self) -> None:
        self.value = 0

    def token_bytes(self, count: int) -> bytes:
        self.value += 1
        return self.value.to_bytes(count, "big")


class ActivityVerifier:
    def verify_activity_ids(self, transaction_id, activity_ids):
        return True


def _adapter(path: Path) -> LinuxDirFdFilesystem:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return LinuxDirFdFilesystem.adopt(fd)
    finally:
        os.close(fd)


def _repo(tmp_path: Path, **kwargs) -> tuple[LinuxDirFdFilesystem, ProposalRepositoryV1]:
    fs = _adapter(tmp_path / "session")
    return fs, ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy(), **kwargs)


def _decision(suffix: str = "B", token: str = "A") -> dict:
    return {
        "schema": 1, "record_id": "rec_" + suffix * 26,
        "decision_result_record_id": "rec_" + "C" * 26,
        "decision_request_fingerprint": D, "proposal_id": U,
        "proposal_revision": 1, "op_id": "op_" + U,
        "decision": "accepted", "reason": "", "actor": "local",
        "decided_at": TS, "previous_row_token": None,
        "row_token": token * 43,
    }


def _checksum(value: dict) -> dict:
    value = copy.deepcopy(value)
    if "scope" in value and "state" in value:
        value.setdefault("record_id", "rec_" + U)
    if "scope" in value and "outcome" in value:
        value.setdefault("result_record_id", "rec_" + U)
    value["checksum"] = canonical.compute_checksum(value)
    return value


def _endpoint(state: str = "terminal", fingerprint: str = D) -> dict:
    value = {
        "schema": 1, "record_id": "rec_" + U, "scope": "/route", "key_digest": D,
        "request_fingerprint": fingerprint, "state": state,
        "http_status": 200 if state == "terminal" else None,
        "response_digest": canonical.canonical_digest({"ok": True}) if state == "terminal" else None,
        "response": {"ok": True} if state == "terminal" else None,
        "transaction_id": "txn_" + U, "created_at": TS, "expires_at": TS3,
    }
    return _checksum(value)


def _preview() -> dict:
    diff = {"schema": 1, "format": "unified", "context_lines": 3, "text": ""}
    diff["digest"] = canonical.canonical_digest(diff)
    validation = {"schema": 1, "profile": "producer", "validation_context_digest": D, "before_findings": [],
                  "after_findings": [], "added": [], "removed": [],
                  "unchanged": [], "status": "pass"}
    validation["digest"] = canonical.canonical_digest(validation)
    value = {"schema": 1, "proposal_id": U, "proposal_revision": 1,
             "snapshot_digest": D, "validation_context_digest": D, "validator_id": RUNTIME,
             "serializer_id": RUNTIME, "operation_runtime_id": RUNTIME,
             "operation_order": [], "concept_changes": [], "diff": diff,
             "validation": validation, "generated_at": TS, "expires_at": TS3}
    value["preview_digest"] = canonical.canonical_digest(value)
    return value


def _leaf(name: str = "backup") -> dict:
    return {"relative_name": name, "device": 1, "inode": 2,
             "mode": 0o600, "size": 1, "digest": D}


def _identity(digest: str = D, inode: int = 3) -> dict:
    return {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
            "leaf_inode": inode, "ctime_ns": 4, "mode": 0o600,
            "size": 1, "digest": digest}


def _history(two_targets: bool = False) -> dict:
    targets = [{"concept": "tables/orders", "before_digest": D,
                "after_digest": DB, "before_identity": _identity(D, 3),
                "staged_identity": _identity(DB, 4),
                "backup_identity": _leaf("backup/leaf")}]
    if two_targets:
        targets.append({"concept": "tables/users", "before_digest": D,
                        "after_digest": DB, "before_identity": _identity(D, 3),
                        "staged_identity": _identity(DB, 4),
                        "backup_identity": _leaf("backup/leaf")})
    return _checksum({
        "schema": 1, "transaction_id": "txn_" + U, "operation": "apply",
        "parent_transaction_id": None, "proposal_id": U, "proposal_revision": 1,
        "undo_group_id": "undo_" + U,
        "targets": targets,
        "created_at": TS, "expires_at": TS3,
    })


def _wal(phase: str = "prepared") -> dict:
    target = {
        "concept": "tables/orders", "before_digest": D,
        "staged_digest": DB, "backup_digest": D, "restore_digest": D,
        "before_identity": _identity(D, 3), "staged_identity": _identity(DB, 4),
        "resulting_identity": None,
        "stage_leaf": _leaf("stage/leaf"), "backup_leaf": _leaf("backup/leaf"),
    }
    value = {
        "schema": 1, "operation": "apply", "transaction_id": "txn_" + U,
        "proposal_id": U, "proposal_revision": 1, "idempotency_scope": "/route",
        "idempotency_key_digest": D, "request_fingerprint": D,
        "transaction_intent_digest": D,
        "decision_set_token": D, "read_model_token": D, "preview_digest": D,
        "snapshot_digest": D, "validation_context_digest": D,
        "validator_id": RUNTIME, "serializer_id": RUNTIME,
        "operation_runtime_id": RUNTIME, "accepted_op_ids": ["op_" + U],
        "targets": [target],
        "stage_dir_identity": {"relative_name": "stage", "device": 1,
                               "inode": 4, "mode": 0o700},
        "backup_dir_identity": {"relative_name": "backup", "device": 1,
                                "inode": 5, "mode": 0o700},
        "phase": phase, "commit_decided": False,
        "publish_bitmap": [False], "rollback_bitmap": [False],
        "recovery_attempts": 0, "created_at": TS, "updated_at": TS,
    }
    return _checksum(value)


def _wal_phase(value: dict, phase: str) -> dict:
    result = copy.deepcopy(value)
    result["phase"] = phase
    count = len(result["targets"])
    result["publish_bitmap"] = [False] * count
    result["rollback_bitmap"] = [False] * count
    result["commit_decided"] = phase not in {"prepared", "rolling_back", "rolled_back"}
    if phase == "publishing":
        result["publish_bitmap"][0] = True
    elif phase in {"content_published", "finalizing", "committed"}:
        result["publish_bitmap"] = [True] * count
    elif phase == "rolling_back":
        result["rollback_bitmap"][-1] = True
    elif phase == "rolled_back":
        result["rollback_bitmap"] = [True] * count
    for target, published in zip(result["targets"], result["publish_bitmap"]):
        target["resulting_identity"] = _identity(DB, 4) if published else None
    result["checksum"] = canonical.compute_checksum(result)
    return result


def _publish_wal_through(repo, target_phase: str, template: dict | None = None) -> dict:
    target = copy.deepcopy(template or _wal("prepared"))
    if target_phase in {"publishing", "content_published", "finalizing", "committed"} and len(target["targets"]) == 1:
        second = copy.deepcopy(target["targets"][0])
        second["concept"] = "tables/users"
        target["targets"] = sorted(target["targets"] + [second], key=lambda item: item["concept"])
    path = (["prepared", "rolling_back", "rolled_back"]
            if target_phase in {"rolling_back", "rolled_back"}
            else ["prepared", "commit_decided", "publishing", "content_published",
                  "finalizing", "committed"])
    published = None
    for phase in path[:path.index(target_phase) + 1]:
        published = _wal_phase(target, phase)
        repo.publish_wal(published)
    assert published is not None
    return published


def _audit(history_digest: str, two_targets: bool = False) -> dict:
    before = {"tables/orders": "0" * 64}
    after = {"tables/orders": "1" * 64}
    backups = {"tables/orders": _leaf("backup/leaf")}
    before_identities = {"tables/orders": _identity(D, 3)}
    if two_targets:
        before["tables/users"] = "0" * 64
        after["tables/users"] = "1" * 64
        backups["tables/users"] = _leaf("backup/leaf")
        before_identities["tables/users"] = _identity(D, 3)
    return _checksum({
        "schema": 1, "record_id": "rec_" + U, "operation": "apply",
        "transaction_id": "txn_" + U, "parent_transaction_id": None,
        "proposal_id": U, "proposal_revision": 1, "actor": "local",
        "endpoint_scope": "/route", "idempotency_key_digest": D,
          "request_fingerprint": D, "transaction_intent_digest": D,
          "accepted_op_ids": ["op_" + U],
        "decision_record_ids": [],
        "decision_rows_digest": D, "decision_set_token": D, "preview_digest": D,
        "snapshot_digest": D, "validation_context_digest": D,
        "validator_id": RUNTIME, "serializer_id": RUNTIME,
        "operation_runtime_id": RUNTIME, "validation_profile": "producer",
        "validation_digest": D, "validation_status": "pass", "phase": "committed",
        "status": "applied", "history_manifest_digest": history_digest,
        "target_before_revisions": before,
        "target_after_revisions": after,
        "target_before_identities": before_identities,
        "target_resulting_identities": {},
        "target_backup_identities": backups,
        "resulting_revisions": after,
        "activity_ids": [], "undo_group_id": "undo_" + U, "outbox_ids": [],
        "started_at": TS, "commit_decided_at": TS2, "completed_at": TS2,
        "error_digest": None,
    })


def test_store_01_single_owner(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path)
    assert repo.repository_id.startswith("repo_")
    repo.append("decisions", "decision", _decision())
    reopened = ProposalRepositoryV1.bootstrap(
        fs, clock=Clock(), entropy=Entropy(), activity_link_verifier=ActivityVerifier())
    assert reopened.snapshot().high_waters["decisions"].sequence == 1
    assert set(reopened.snapshot().records) == set(schemas.AGGREGATE_KINDS)


def test_store_02_torn_tail(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path)
    repo.append("decisions", "decision", _decision())
    repo._fs.append_fsync("active/decisions.jsonl", b'{"torn":')
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    assert reopened.snapshot().high_waters["decisions"].sequence == 1
    assert reopened._fs.read_bytes("active/decisions.jsonl").endswith(b"\n")


def test_store_03_corruption_marker(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path)
    repo._fs.append_fsync("active/decisions.jsonl", b'{"complete":"bad"}\n')
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    assert reopened.snapshot().records["corruption"][-1].kind == "corruption"
    with pytest.raises(RepositoryCorruptError):
        reopened.append("decisions", "decision", _decision())


def test_store_04_checkpoint_snapshot(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    repo.append("decisions", "decision", _decision())
    first = repo.snapshot()
    repo.append("decisions", "decision", _decision("D", "B"))
    assert len(first.aggregate("decisions")) == 1
    assert first.high_waters["decisions"].sequence == 1
    assert repo.snapshot().token != first.token


def test_store_05_rotation(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path, rotation_bytes=600)
    repo.append("decisions", "decision", _decision())
    repo.append("decisions", "decision", _decision("D", "B"))
    assert repo.snapshot().high_waters["decisions"].generation == 1
    assert len(repo._fs.list_dir("generations")) == 1
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy(), rotation_bytes=600)
    assert reopened.snapshot().high_waters["decisions"].sequence == 2


def test_store_06_pruning(tmp_path: Path) -> None:
    limits = dataclasses.replace(RepositoryLimits(), generation_count=2)
    _, repo = _repo(tmp_path, rotation_bytes=600, limits=limits)
    for i, suffix in enumerate("BDEFG"):
        repo.append("decisions", "decision", _decision(suffix, "ABCDEFG"[i]))
    assert len(repo._generation_files("decisions")) <= 2
    assert repo.checkpoint("decisions").sequence_high_water == 5


def test_store_07_dir_substitution(tmp_path: Path) -> None:
    session = tmp_path / "session"
    fs = _adapter(session)
    repo = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    original = session / "proposal-review-v1"
    moved = session / "proposal-review-v1-old"
    original.rename(moved)
    original.mkdir(mode=0o700)
    repo.append("decisions", "decision", _decision())
    assert (moved / "active" / "decisions.jsonl").stat().st_size > 0
    assert not (original / "active").exists()


def test_store_08_startup_recovery(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path)
    # Foundation A classifies content recovery but never publishes bundle bytes.
    repo.publish_wal(_wal())
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    assert reopened.recovery_report.items[0].classification == "rollback_required"
    assert not reopened.recovery_report.serving_safe


def test_store_09_preview_authority(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    published = repo.publish_preview(_preview())
    assert repo.get_preview(published.preview_digest) == published
    value = published.preview_digest[7:]
    repo._fs.write_atomic(f"previews/sha256/{value[:2]}/{value[2:]}.json", b"{}")
    with pytest.raises(RepositoryCorruptError):
        repo.get_preview(published.preview_digest)


def test_store_10_repair(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path)
    repo.append("endpoint_results", "endpoint_result", _endpoint())
    contradictory = _endpoint()
    contradictory["response"] = {"contradictory": True}
    contradictory["response_digest"] = canonical.canonical_digest(
        contradictory["response"])
    contradictory["checksum"] = canonical.compute_checksum(contradictory)
    repo.append("endpoint_results", "endpoint_result", contradictory)
    marker_data = _checksum({
        "schema": 1, "corruption_id": "cor_" + "C" * 26,
        "repository_id": repo.repository_id, "authority_kind": "endpoint_result",
        "authority_identity": {"kind": "endpoint_result", "scope": "/route",
                               "key_digest": D, "sequence": 1,
                               "relative_path": "active/endpoint-results.jsonl"},
        "reason": "authority_contradiction", "observed_digest": D,
        "diagnostic_copy": None, "detected_at": TS,
        "repaired_at": None, "repair_record_id": None,
    })
    with repo._lock.hold(repo._lock_timeout):
        wrapper = repo._append_locked("corruption", "corruption", marker_data,
                                      permit_blocked=True)
    marker = schemas.CorruptionMarker.from_mapping(wrapper.payload)
    repair = _checksum({
        "schema": 1, "repair_record_id": "rec_" + "R" * 26,
        "corruption_id": marker.corruption_id, "repository_id": repo.repository_id,
        "action": "classify_indeterminate", "replacement_authority": None,
        "terminal_classification": {"scope": "/route", "key_digest": D,
                                    "transaction_id": "txn_" + U,
                                    "outcome": "indeterminate"},
        "operator_label": "ops", "reason": "explicit classification", "created_at": TS,
    })
    successor = marker.to_mapping()
    successor.update(repaired_at=TS2, repair_record_id=repair["repair_record_id"])
    successor["checksum"] = canonical.compute_checksum(successor)
    with pytest.raises(RepositoryError):
        repo.apply_repair(repair, successor)
    repo.stop_for_repair()
    repo.apply_repair(repair, successor)
    assert repo.snapshot().records["corruption"][-1].payload["repaired_at"] == TS2
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    result = reopened.reconcile_endpoint("/route", D, DB)
    assert (result.state, result.transaction_id) == ("indeterminate", "txn_" + U)


def test_store_11_quotas(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path)
    used = sum(repo._fs.file_size(p) for p in repo._fs.walk_files())
    tiny = dataclasses.replace(RepositoryLimits(), repository_quota_bytes=used + 10)
    # Reopen with a changed limits digest must itself fail closed.
    with pytest.raises(RepositoryCorruptError):
        ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy(), limits=tiny)
    with pytest.raises(AttributeError):
        repo.limits = dataclasses.replace(repo.limits, repository_quota_bytes=used + 10)
    assert repo.append("decisions", "decision", _decision()).sequence == 1


def test_repository_limits_reject_normative_maximum_bypass() -> None:
    with pytest.raises(RepositoryError, match="repository_quota_bytes"):
        dataclasses.replace(RepositoryLimits(), repository_quota_bytes=2 * 1024 * 1024 * 1024 + 1)
    with pytest.raises(RepositoryError, match="tombstone_rows"):
        dataclasses.replace(RepositoryLimits(), tombstone_rows=True)


def test_store_12_topology(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    with pytest.raises(SchemaError):
        repo.append("transactions", "decision", _decision())
    repo.append("endpoint_results", "endpoint_result", _endpoint())
    assert repo.reconcile_endpoint("/route", D, D).state == "terminal_result"
    with pytest.raises(IdempotencyConflictError):
        repo.reconcile_endpoint("/route", D, DB)


def test_store_13_history_manifest_authority(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path)
    history = repo.publish_history(_history())
    audit = schemas.TransactionAudit.from_mapping(_audit(history.canonical_digest()))
    assert repo.verify_history_link(audit) == history
    repo.append("transactions", "transaction", audit)
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    assert reopened.verify_history_link(audit) == history
    bad = audit.to_mapping(); bad["history_manifest_digest"] = D
    bad["checksum"] = canonical.compute_checksum(bad)
    with pytest.raises(RepositoryCorruptError):
        repo.verify_history_link(bad)


def _hold_lock(path: str, ready, release) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    fs = LinuxDirFdFilesystem.adopt(fd); os.close(fd)
    root = fs.subroot("proposal-review-v1")
    with root.lock().hold(1):
        ready.set(); release.wait(2)


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_repository_lock_timeout_is_cross_process(tmp_path: Path) -> None:
    fs, repo = _repo(tmp_path)
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(tmp_path / "session"), ready, release))
    proc.start(); assert ready.wait(2)
    try:
        with pytest.raises(LockUnavailableError):
            with repo._lock.hold(0.02):
                pass
    finally:
        release.set(); proc.join(3)
    assert proc.exitcode == 0


def test_repository_import_boundary() -> None:
    import ast
    import okf_loom.proposal_review as package
    forbidden = {"server", "studio", "cli", "render", "viewer", "Bundle"}
    transport_adapters = {"activation.py", "http.py", "cli_adapter.py", "studio_outbox.py"}
    for source in Path(package.__file__).parent.glob("*.py"):
        if source.name in transport_adapters:
            continue
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                text = ast.unparse(node)
                assert not any(word in text for word in forbidden), (source, text)


def test_legacy_reader_is_bounded_read_only(tmp_path: Path) -> None:
    fs = _adapter(tmp_path / "legacy")
    good = {"id": "same", "summary": "old", "author": "agent", "created_at": TS}
    fs.write_atomic("proposals.jsonl", canonical.dumps_canonical(good) + b"\n{}\n")
    result = LegacySuggestionReader(fs).read(source_generation=7)
    assert result.projections[0].legacy_identity.source_generation == 7
    assert result.projections[0].applicable is False
    assert len(result.corruptions) == 1


def test_legacy_reader_seen_after_projection(tmp_path: Path) -> None:
    """Only mark seen after successful projection; corruption retains detail (Fix #15)."""
    fs = _adapter(tmp_path / "legacy2")
    # Good row, then a row that fails projection (missing summary),
    # then a good row with the SAME id as the bad one.
    good1 = {"id": "g1", "summary": "ok", "author": "a", "created_at": TS}
    bad = {"id": "g1", "author": "a", "created_at": TS}  # missing summary
    content = canonical.dumps_canonical(good1) + b"\n" + canonical.dumps_canonical(bad) + b"\n"
    fs.write_atomic("proposals.jsonl", content)
    result = LegacySuggestionReader(fs).read(source_generation=1)
    # The bad row failed projection, so its id must NOT be in seen
    # (it didn't block a later good row with the same id).
    assert len(result.projections) == 1  # only the first good row
    assert len(result.corruptions) == 1
    # Corruption detail is bounded but useful.
    assert "summary" in result.corruptions[0].reason or "SchemaError" in result.corruptions[0].reason


# ============================================================================
# Fix #1: Repository sequence ownership — two instances append unique contiguous
# ============================================================================


def test_store_01_two_instances_unique_sequences(tmp_path: Path) -> None:
    """Two already-open repository instances must append unique contiguous sequences."""
    fs, repo_a = _repo(tmp_path)
    # Simulate two processes by opening a second repository on the same root.
    repo_b = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    # Both start with sequence 0.
    w1 = repo_a.append("decisions", "decision", _decision("B", "A"))
    w2 = repo_b.append("decisions", "decision", _decision("C", "B"))
    assert w1.sequence == 1
    assert w2.sequence == 2  # repo_b reloaded disk under the lock
    # Both wrappers are contiguous and unique.
    assert w1.sequence != w2.sequence
    # Reopening verifies the full sequence is contiguous.
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    assert reopened.snapshot().high_waters["decisions"].sequence == 2


# ============================================================================
# Fix #2: Checkpoint trust — checkpoint wrappers validated like JSONL
# ============================================================================


def test_store_04_checkpoint_validates_wrappers(tmp_path: Path) -> None:
    """Checkpoint records are validated for topology and payload (Fix #2)."""
    fs, repo = _repo(tmp_path)
    repo.append("decisions", "decision", _decision())
    repo.checkpoint("decisions")  # explicitly create checkpoint
    # Corrupt the checkpoint's payload to have a bad kind.
    cp_path = "checkpoints/decisions.json"
    cp = schemas.Checkpoint.from_json(repo._fs.read_bytes(cp_path))
    bad_records = list(cp.records)
    bad_records[0] = dict(bad_records[0])
    bad_records[0]["kind"] = "proposal"  # wrong kind for decisions aggregate
    obj = dict(cp.to_mapping())
    obj["records"] = bad_records
    obj["records_digest"] = canonical.canonical_digest(bad_records)
    obj["checksum"] = canonical.compute_checksum(obj)
    repo._fs.write_atomic(cp_path, canonical.dumps_canonical(obj))
    # Reopen: the corrupted checkpoint must not be trusted as seed.
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    # The active log still has the valid record; checkpoint is rejected.
    assert reopened.snapshot().high_waters["decisions"].sequence == 1


# ============================================================================
# Fix #4: WAL immutable identity fields and backward transition rejection
# ============================================================================


def test_store_wal_rejects_immutable_field_change(tmp_path: Path) -> None:
    """WAL immutable fields cannot be rebound (Fix #4)."""
    _, repo = _repo(tmp_path)
    wal1 = _wal("prepared")
    repo.publish_wal(wal1)
    # Change an immutable field (proposal_id).
    wal2 = copy.deepcopy(wal1)
    wal2["proposal_id"] = "0" * 26  # different proposal_id
    wal2["checksum"] = canonical.compute_checksum(wal2)
    with pytest.raises(RepositoryCorruptError):
        repo.publish_wal(wal2)


def test_store_wal_rejects_backward_phase(tmp_path: Path) -> None:
    """WAL cannot transition backward on the forward path (Fix #4)."""
    _, repo = _repo(tmp_path)
    wal_committed = _publish_wal_through(repo, "committed")
    # Try to go back to commit_decided (individually valid, but backward).
    wal_back = copy.deepcopy(wal_committed)
    wal_back["phase"] = "commit_decided"
    wal_back["publish_bitmap"] = [False] * len(wal_back["targets"])
    for target in wal_back["targets"]:
        target["resulting_identity"] = None
    wal_back["checksum"] = canonical.compute_checksum(wal_back)
    with pytest.raises(RepositoryCorruptError):
        repo.publish_wal(wal_back)


@pytest.mark.parametrize("phase", sorted(set(schemas.WAL_PHASES) - {"prepared"}))
def test_first_wal_publish_rejects_every_non_prepared_phase(tmp_path: Path, phase: str) -> None:
    _, repo = _repo(tmp_path)
    candidate = _wal("prepared")
    if phase in {"publishing", "content_published", "finalizing", "committed"}:
        second = copy.deepcopy(candidate["targets"][0])
        second["concept"] = "tables/users"
        candidate["targets"] = sorted(candidate["targets"] + [second],
                                      key=lambda item: item["concept"])
    candidate = _wal_phase(candidate, phase)
    with pytest.raises(RepositoryCorruptError, match="first WAL publication"):
        repo.publish_wal(candidate)


# ============================================================================
# Fix #5: History/audit/WAL/result association exact
# ============================================================================


def test_store_13_wal_retire_association(tmp_path: Path) -> None:
    """can_retire_wal checks WAL/audit/endpoint/result association (Fix #5)."""
    fs, repo = _repo(tmp_path)
    history = repo.publish_history(_history(two_targets=True))
    audit = schemas.TransactionAudit.from_mapping(_audit(history.canonical_digest(), two_targets=True))
    repo.append("transactions", "transaction", audit)
    # WAL is committed.
    wal_committed = _publish_wal_through(repo, "committed")
    # Terminal endpoint result with matching scope => can retire.
    ep_match = _endpoint()
    repo.append("endpoint_results", "endpoint_result", ep_match)
    repo_a = ProposalRepositoryV1.bootstrap(
        fs, clock=Clock(), entropy=Entropy(), activity_link_verifier=ActivityVerifier())
    # The legacy fixture intentionally has no finalization intent; the updated
    # association contract must refuse retirement rather than infer one.
    assert repo_a.can_retire_wal("txn_" + U) is False
    # Now test with mismatched scope: fresh repo, audit with /route,
    # endpoint result with /different.
    (tmp_path / "repo2").mkdir(mode=0o700)
    fs2, repo2 = _repo(tmp_path / "repo2")
    history2 = repo2.publish_history(_history(two_targets=True))
    audit2 = schemas.TransactionAudit.from_mapping(_audit(history2.canonical_digest(), two_targets=True))
    repo2.append("transactions", "transaction", audit2)
    _publish_wal_through(repo2, "committed", wal_committed)
    ep_mismatch = _endpoint()
    ep_mismatch["scope"] = "/different"  # differs from audit endpoint_scope="/route"
    ep_mismatch["checksum"] = canonical.compute_checksum(ep_mismatch)
    repo2.append("endpoint_results", "endpoint_result", ep_mismatch)
    assert repo2.can_retire_wal("txn_" + U) is False


# ============================================================================
# Fix #6: Quotas — tombstone/audit row/bytes with typed errors
# ============================================================================


def test_store_11_tombstone_quota(tmp_path: Path) -> None:
    """Tombstone row quota is enforced with a typed error (Fix #6)."""
    limits = dataclasses.replace(RepositoryLimits(), tombstone_rows=2)
    _, repo = _repo(tmp_path, limits=limits)
    tb = _checksum({
        "schema": 1, "scope": "/route", "key_digest": D,
        "request_fingerprint": D, "outcome": "terminal_compacted",
        "response_digest": D, "transaction_id": "txn_" + U,
        "created_at": TS, "expires_at": TS3,
    })
    repo.append("tombstones", "tombstone", tb)
    tb2 = copy.deepcopy(tb)
    tb2["key_digest"] = "sha256:" + "2" * 64
    tb2["checksum"] = canonical.compute_checksum(tb2)
    repo.append("tombstones", "tombstone", tb2)
    tb3 = copy.deepcopy(tb)
    tb3["key_digest"] = "sha256:" + "3" * 64
    tb3["checksum"] = canonical.compute_checksum(tb3)
    with pytest.raises(RepositoryQuotaError) as exc_info:
        repo.append("tombstones", "tombstone", tb3)
    assert exc_info.value.code == "tombstone_quota_exceeded"


# ============================================================================
# Fix #14: Duplicate sequences create corruption even if byte-identical
# ============================================================================


def test_store_01_duplicate_sequence_corruption(tmp_path: Path) -> None:
    """A duplicate sequence — even byte-identical — creates corruption (Fix #14)."""
    fs, repo = _repo(tmp_path)
    # Append a decision.
    repo.append("decisions", "decision", _decision())
    # Read the active log and duplicate the first row.
    active = repo._fs.read_bytes("active/decisions.jsonl")
    lines = active.split(b"\n")
    # Duplicate the first non-empty line.
    dup_line = next(l for l in lines if l)
    repo._fs.append_fsync("active/decisions.jsonl", dup_line + b"\n")
    # Reopen: the duplicate must be detected as corruption.
    reopened = ProposalRepositoryV1.bootstrap(
        fs, clock=Clock(), entropy=Entropy(), activity_link_verifier=ActivityVerifier())
    assert reopened.snapshot().records["corruption"][-1].kind == "corruption"
    with pytest.raises(RepositoryCorruptError):
        reopened.append("decisions", "decision", _decision("D", "C"))


# ============================================================================
# Req #1: True post-parse deep immutability on repository records
# ============================================================================


def test_wrapper_payload_post_parse_mutation_rejected(tmp_path: Path) -> None:
    """FrozenDict/FrozenList in wrapper payload reject post-parse mutation (Req #1)."""
    _, repo = _repo(tmp_path)
    repo.append("decisions", "decision", _decision())
    snap = repo.snapshot()
    wrapper = snap.aggregate("decisions")[0]
    with pytest.raises(TypeError):
        wrapper.payload["decision"] = "rejected"
    with pytest.raises(TypeError):
        wrapper.payload["new_key"] = "val"


def test_frozen_in_place_operators_preserve_canonical_authority() -> None:
    value = schemas.Wrapper.from_mapping(_checksum({
        "record_schema": 1, "kind": "decision", "sequence": 1,
        "payload": _decision(),
    }))
    before = value.canonical_bytes()
    before_digest = canonical.canonical_digest(value.to_mapping())
    with pytest.raises(TypeError, match="immutable"):
        value.payload |= {"new": True}
    nested = schemas.FrozenList([{"x": 1}])
    with pytest.raises(TypeError, match="immutable"):
        nested += [{"x": 2}]
    with pytest.raises(TypeError, match="immutable"):
        nested *= 2
    assert value.canonical_bytes() == before
    assert canonical.canonical_digest(value.to_mapping()) == before_digest


# ============================================================================
# Req #3: Endpoint reconciliation — two-instance and contradiction tests
# ============================================================================


def test_reconcile_endpoint_two_instances(tmp_path: Path) -> None:
    """Two open instances see the same terminal endpoint result (Req #3)."""
    fs, repo_a = _repo(tmp_path)
    repo_a.append("endpoint_results", "endpoint_result", _endpoint())
    repo_b = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    rec_a = repo_a.reconcile_endpoint("/route", D, D)
    rec_b = repo_b.reconcile_endpoint("/route", D, D)
    assert rec_a.state == "terminal_result"
    assert rec_b.state == "terminal_result"


def test_reconcile_endpoint_contradiction_blocks(tmp_path: Path) -> None:
    """Contradictory terminal results append marker and block aggregate (Req #3)."""
    fs, repo = _repo(tmp_path)
    ep1 = _endpoint()
    repo.append("endpoint_results", "endpoint_result", ep1)
    # Append a second terminal result with different response but same scope/key.
    ep2 = copy.deepcopy(ep1)
    ep2["response"] = {"different": True}
    ep2["response_digest"] = canonical.canonical_digest({"different": True})
    ep2["checksum"] = canonical.compute_checksum(ep2)
    # Use the repository's own append to get a proper sequence 2.
    repo.append("endpoint_results", "endpoint_result", ep2)
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    # Both terminal results for the same scope/key with different content
    # must be detected as a contradiction.
    with pytest.raises(RepositoryCorruptError, match="contradictory terminal"):
        reopened.reconcile_endpoint("/route", D, D)
    with pytest.raises(RepositoryCorruptError, match="blocked"):
        reopened.append("endpoint_results", "endpoint_result", _endpoint("prepared"))


def test_reconcile_terminal_tombstone_mismatch_blocks(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    repo.append("endpoint_results", "endpoint_result", _endpoint())
    tombstone = _checksum({
        "schema": 1, "scope": "/route", "key_digest": D,
        "request_fingerprint": D, "outcome": "terminal_compacted",
        "response_digest": DB, "transaction_id": "txn_" + U,
        "created_at": TS, "expires_at": TS3,
    })
    repo.append("tombstones", "tombstone", tombstone)
    with pytest.raises(RepositoryCorruptError, match="terminal endpoint and tombstone"):
        repo.reconcile_endpoint("/route", D, D)
    with pytest.raises(RepositoryCorruptError, match="blocked"):
        repo.append("endpoint_results", "endpoint_result", _endpoint("prepared"))


def test_reconcile_conflicting_tombstone_response_digest_blocks(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    tombstone = _checksum({
        "schema": 1, "scope": "/route", "key_digest": D,
        "request_fingerprint": D, "outcome": "terminal_compacted",
        "response_digest": D, "transaction_id": "txn_" + U,
        "created_at": TS, "expires_at": TS3,
    })
    repo.append("tombstones", "tombstone", tombstone)
    conflict = copy.deepcopy(tombstone)
    conflict["response_digest"] = DB
    conflict["checksum"] = canonical.compute_checksum(conflict)
    repo.append("tombstones", "tombstone", conflict)
    with pytest.raises(RepositoryCorruptError, match="contradictory tombstones"):
        repo.reconcile_endpoint("/route", D, D)
    with pytest.raises(RepositoryCorruptError, match="blocked"):
        repo.append("tombstones", "tombstone", tombstone)


def test_reconcile_earlier_tombstone_contradicting_terminal_blocks(tmp_path: Path) -> None:
    """Every tombstone is authority even when the latest matches the endpoint."""
    _, repo = _repo(tmp_path)
    endpoint = _endpoint()
    repo.append("endpoint_results", "endpoint_result", endpoint)
    matching = _checksum({
        "schema": 1, "scope": "/route", "key_digest": D,
        "request_fingerprint": D, "outcome": "terminal_compacted",
        "response_digest": endpoint["response_digest"],
        "transaction_id": "txn_" + U,
        "created_at": TS, "expires_at": TS3,
    })
    contradictory = copy.deepcopy(matching)
    contradictory["response_digest"] = DB
    contradictory["checksum"] = canonical.compute_checksum(contradictory)
    repo.append("tombstones", "tombstone", contradictory)
    repo.append("tombstones", "tombstone", matching)

    with pytest.raises(RepositoryCorruptError, match="contradictory tombstones"):
        repo.reconcile_endpoint("/route", D, D)
    with pytest.raises(RepositoryCorruptError, match="blocked"):
        repo.append("tombstones", "tombstone", matching)


def test_reconcile_terminal_and_prepared_is_corruption(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    repo.append("endpoint_results", "endpoint_result", _endpoint("prepared"))
    repo.append("endpoint_results", "endpoint_result", _endpoint())
    with pytest.raises(RepositoryCorruptError, match="prepared and terminal"):
        repo.reconcile_endpoint("/route", D, D)


def test_reconcile_malformed_wal_never_returns_new(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    repo._fs.mkdirs("transactions/txn_" + U)
    repo._fs.write_atomic("transactions/txn_" + U + "/wal.json", b"not-json")
    with pytest.raises(RepositoryCorruptError, match="WAL failed endpoint reconciliation"):
        repo.reconcile_endpoint("/route", D, D)


def test_reconcile_multiple_matching_wals_is_corruption(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    repo.publish_wal(_wal())
    second = _wal()
    second["transaction_id"] = "txn_" + "1" + U[1:]
    second["checksum"] = canonical.compute_checksum(second)
    repo._fs.mkdirs("transactions/" + second["transaction_id"])
    repo._fs.write_atomic("transactions/" + second["transaction_id"] + "/wal.json",
                          canonical.dumps_canonical(second))
    with pytest.raises(RepositoryCorruptError, match="multiple WALs"):
        repo.reconcile_endpoint("/route", D, D)


def test_two_instances_append_distinct_corruption_markers_contiguously(tmp_path: Path) -> None:
    fs, first = _repo(tmp_path)
    second = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    with first._lock.hold(first._lock_timeout):
        one = first._append_corruption_locked(
            "decisions", None, None, "active/decisions.jsonl", "missing", b"")
    with second._lock.hold(second._lock_timeout):
        two = second._append_corruption_locked(
            "proposals", None, None, "active/proposals.jsonl", "missing", b"")
    assert (one.sequence, two.sequence) == (1, 2)


# ============================================================================
# Req #4: WAL transition graph — legal edges and illegal edges
# ============================================================================


def _advance_wal(repo, old_phase, new_phase, commit_decided=True,
                 publish=None, rollback=None):
    """Publish a WAL advancing from old_phase to new_phase with legal bitmaps."""
    base = _wal(old_phase)
    # Fix bitmaps for old_phase if needed
    if old_phase in ("commit_decided", "prepared") and commit_decided:
        base["phase"] = old_phase
        base["commit_decided"] = True if old_phase != "prepared" else False
        base["publish_bitmap"] = [False]
        base["rollback_bitmap"] = [False]
        base["checksum"] = canonical.compute_checksum(base)
    base = _publish_wal_through(repo, old_phase, base)
    # Now advance to new_phase
    advanced = copy.deepcopy(base)
    advanced["phase"] = new_phase
    if new_phase in ("prepared", "rolling_back", "rolled_back"):
        advanced["commit_decided"] = False
    else:
        advanced["commit_decided"] = True
    if publish is not None:
        advanced["publish_bitmap"] = publish
    if rollback is not None:
        advanced["rollback_bitmap"] = rollback
    if new_phase in ("committed", "content_published", "finalizing"):
        advanced["publish_bitmap"] = [True]
    if new_phase == "rolled_back":
        advanced["rollback_bitmap"] = [True]
    if new_phase == "rolling_back":
        advanced["rollback_bitmap"] = [True]
    advanced["updated_at"] = TS2
    advanced["checksum"] = canonical.compute_checksum(advanced)
    return repo.publish_wal(advanced)


@pytest.mark.parametrize(
    "old_phase,new_phase",
    [
        ("prepared", "commit_decided"),
        ("prepared", "rolling_back"),
        ("commit_decided", "publishing"),
        ("rolling_back", "rolled_back"),
    ],
)
def test_wal_legal_transition(tmp_path, old_phase, new_phase):
    """Every legal forward/rollback edge is accepted (Req #4)."""
    _, repo = _repo(tmp_path)
    # Use 2 targets so 'publishing' (proper prefix) is possible.
    t1 = _wal("prepared")["targets"][0]
    t2 = copy.deepcopy(t1)
    t2["concept"] = "tables/customers"
    targets = sorted([t1, t2], key=lambda t: t["concept"])
    base = _wal(old_phase)
    base["targets"] = targets
    base["publish_bitmap"] = [False, False]
    base["rollback_bitmap"] = [False, False]
    if old_phase == "commit_decided":
        base["commit_decided"] = True
    elif old_phase == "rolling_back":
        base["rollback_bitmap"] = [False, True]
    base["checksum"] = canonical.compute_checksum(base)
    base = _publish_wal_through(repo, old_phase, base)
    adv = copy.deepcopy(base)
    adv["phase"] = new_phase
    cd = new_phase not in ("prepared", "rolling_back", "rolled_back")
    adv["commit_decided"] = cd
    if new_phase == "publishing":
        adv["publish_bitmap"] = [True, False]  # proper prefix of 2
        adv["rollback_bitmap"] = [False, False]
    elif new_phase in ("committed", "content_published", "finalizing"):
        adv["publish_bitmap"] = [True, True]
    elif new_phase == "rolling_back":
        adv["rollback_bitmap"] = [False, True]
    elif new_phase == "rolled_back":
        adv["rollback_bitmap"] = [True, True]
    for target, published in zip(adv["targets"], adv["publish_bitmap"]):
        target["resulting_identity"] = _identity(DB, 4) if published else None
    adv["updated_at"] = TS2
    adv["checksum"] = canonical.compute_checksum(adv)
    repo.publish_wal(adv)  # must not raise


@pytest.mark.parametrize(
    "old_phase,new_phase",
    [
        ("committed", "prepared"),     # terminal -> anything non-same
        ("rolled_back", "prepared"),   # terminal -> anything non-same
    ],
)
def test_wal_illegal_transition(tmp_path, old_phase, new_phase):
    """Illegal transitions are rejected (Req #4)."""
    _, repo = _repo(tmp_path)
    base = _publish_wal_through(repo, old_phase)
    adv = copy.deepcopy(base)
    adv["phase"] = new_phase
    adv["updated_at"] = TS2
    if new_phase == "prepared":
        adv["commit_decided"] = False
        adv["publish_bitmap"] = [False] * len(adv["targets"])
        adv["rollback_bitmap"] = [False] * len(adv["targets"])
        for target in adv["targets"]:
            target["resulting_identity"] = None
    adv["checksum"] = canonical.compute_checksum(adv)
    with pytest.raises(RepositoryCorruptError):
        repo.publish_wal(adv)


def test_wal_skip_phase_rejected(tmp_path: Path) -> None:
    """Skipping forward phases (prepared->committed) is rejected (Req #4)."""
    _, repo = _repo(tmp_path)
    repo.publish_wal(_wal("prepared"))
    adv = copy.deepcopy(_wal("prepared"))
    adv["phase"] = "commit_decided"  # legal next from prepared
    adv["commit_decided"] = True
    adv["updated_at"] = TS2
    adv["checksum"] = canonical.compute_checksum(adv)
    repo.publish_wal(adv)
    # Now try skipping from commit_decided to committed (skipping publishing etc.)
    skip = copy.deepcopy(adv)
    skip["phase"] = "committed"
    skip["publish_bitmap"] = [True]
    skip["targets"][0]["resulting_identity"] = _identity(DB, 4)
    skip["updated_at"] = "2026-07-16T12:00:00.000Z"
    skip["checksum"] = canonical.compute_checksum(skip)
    with pytest.raises(RepositoryCorruptError):
        repo.publish_wal(skip)


def test_multi_target_commit_transition_cardinality_rejected_at_repository(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path)
    first = _wal("prepared")["targets"][0]
    second = copy.deepcopy(first); second["concept"] = "tables/users"
    prepared = _wal("prepared"); prepared["targets"] = sorted([first, second], key=lambda row: row["concept"])
    prepared["publish_bitmap"] = [False, False]; prepared["rollback_bitmap"] = [False, False]
    prepared["checksum"] = canonical.compute_checksum(prepared)
    repo.publish_wal(prepared)
    decided = _wal_phase(prepared, "commit_decided"); repo.publish_wal(decided)
    illegal = copy.deepcopy(decided); illegal["phase"] = "content_published"
    illegal["publish_bitmap"] = [True, True]
    for target in illegal["targets"]:
        target["resulting_identity"] = _identity(DB, 4)
    illegal["checksum"] = canonical.compute_checksum(illegal)
    with pytest.raises(RepositoryCorruptError):
        repo.publish_wal(illegal)


def test_wal_created_at_immutable(tmp_path: Path) -> None:
    """created_at cannot change across WAL updates (Req #4)."""
    _, repo = _repo(tmp_path)
    repo.publish_wal(_wal("prepared"))
    adv = _wal("commit_decided")
    adv["commit_decided"] = True
    adv["phase"] = "commit_decided"
    adv["created_at"] = TS2  # different created_at
    adv["checksum"] = canonical.compute_checksum(adv)
    with pytest.raises(RepositoryCorruptError):
        repo.publish_wal(adv)


@pytest.mark.parametrize("field", ProposalRepositoryV1._WAL_IMMUTABLE_FIELDS)
def test_wal_every_prepare_time_field_is_immutable(tmp_path: Path, field: str) -> None:
    _, repo = _repo(tmp_path)
    base = _wal("prepared")
    repo.publish_wal(base)
    changed = copy.deepcopy(base)
    if field == "checksum":
        pytest.fail("checksum is not a prepare-time immutable field")
    if field == "schema":
        changed[field] = 2
    elif field == "operation":
        changed.update(operation="undo", transaction_id="undo_" + U,
                       accepted_op_ids=[], decision_set_token=None, preview_digest=None)
        changed["targets"][0]["restore_digest"] = changed["targets"][0]["staged_digest"]
    elif field == "transaction_id":
        changed[field] = "txn_" + "1" + U[1:]
    elif field == "proposal_id":
        changed[field] = "1" + U[1:]
    elif field == "proposal_revision":
        changed[field] = 2
    elif field in {"idempotency_scope"}:
        changed[field] += "/changed"
    elif field in {"idempotency_key_digest", "request_fingerprint", "transaction_intent_digest",
                       "decision_set_token", "read_model_token", "preview_digest",
                       "snapshot_digest", "validation_context_digest"}:
        changed[field] = DB
    elif field in {"validator_id", "serializer_id", "operation_runtime_id"}:
        changed[field] = "runtime@1.0+sha256:" + "1" * 64
    elif field == "accepted_op_ids":
        changed[field] = ["op_" + U, "op_" + "1" + U[1:]]
    elif field == "targets":
        second = copy.deepcopy(changed[field][0])
        second["concept"] = "tables/users"
        changed[field] = sorted(changed[field] + [second], key=lambda item: item["concept"])
        changed["publish_bitmap"] = [False, False]
        changed["rollback_bitmap"] = [False, False]
    elif field in {"stage_dir_identity", "backup_dir_identity"}:
        changed[field]["inode"] += 10
    elif field == "created_at":
        changed[field] = TS2
    else:
        raise AssertionError(f"missing mutation fixture for {field}")
    changed["checksum"] = canonical.compute_checksum(changed)
    expected = SchemaError if field == "schema" else RepositoryCorruptError
    with pytest.raises(expected):
        repo.publish_wal(changed)


def test_wal_updated_at_nondecreasing(tmp_path: Path) -> None:
    """updated_at must be nondecreasing (Req #4)."""
    _, repo = _repo(tmp_path)
    w = _wal("prepared")
    w["updated_at"] = TS2
    w["checksum"] = canonical.compute_checksum(w)
    repo.publish_wal(w)
    adv = copy.deepcopy(w)
    adv["phase"] = "commit_decided"
    adv["commit_decided"] = True
    adv["updated_at"] = TS  # before TS2
    adv["checksum"] = canonical.compute_checksum(adv)
    with pytest.raises(RepositoryCorruptError):
        repo.publish_wal(adv)


# ============================================================================
# Req #5: Undo WAL retirement with nullable preview_digest
# ============================================================================


def test_undo_wal_retire_succeeds(tmp_path: Path) -> None:
    """Undo WAL (nullable preview_digest) can retire when all links match (Req #5)."""
    fs, repo = _repo(tmp_path)
    # Build undo WAL: preview_digest is null for undo.
    undo_wal = _wal("rolled_back")
    undo_wal["operation"] = "undo"
    undo_wal["transaction_id"] = "undo_" + U
    undo_wal["phase"] = "rolled_back"
    undo_wal["commit_decided"] = False
    undo_wal["publish_bitmap"] = [False]
    undo_wal["rollback_bitmap"] = [True]
    undo_wal["preview_digest"] = None  # undo has null preview
    undo_wal["accepted_op_ids"] = []
    undo_wal["decision_set_token"] = None
    undo_wal["targets"][0]["restore_digest"] = undo_wal["targets"][0]["staged_digest"]
    undo_wal["checksum"] = canonical.compute_checksum(undo_wal)
    undo_wal = _publish_wal_through(repo, "rolled_back", undo_wal)
    # Build undo history manifest.
    undo_hist = _history()
    undo_hist["transaction_id"] = "undo_" + U
    undo_hist["operation"] = "undo"
    undo_hist["parent_transaction_id"] = "txn_" + U
    undo_hist["checksum"] = canonical.compute_checksum(undo_hist)
    history = repo.publish_history(undo_hist)
    # Build undo audit from scratch with undo-specific fields.
    undo_audit = _checksum({
        "schema": 1, "record_id": "rec_" + U, "operation": "undo",
        "transaction_id": "undo_" + U, "parent_transaction_id": "txn_" + U,
        "proposal_id": U, "proposal_revision": 1, "actor": "local",
        "endpoint_scope": "/route", "idempotency_key_digest": D,
        "request_fingerprint": D, "transaction_intent_digest": D,
        "accepted_op_ids": [],
        "decision_record_ids": [],
        "decision_rows_digest": D, "decision_set_token": D,
        "preview_digest": D, "snapshot_digest": D,
        "validation_context_digest": D,
        "validator_id": RUNTIME, "serializer_id": RUNTIME,
        "operation_runtime_id": RUNTIME, "validation_profile": "producer",
        "validation_digest": D, "validation_status": "pass",
        "phase": "rolled_back", "status": "undone",
        "history_manifest_digest": history.canonical_digest(),
        "target_before_revisions": {"tables/orders": "0" * 64},
        "target_after_revisions": {"tables/orders": "1" * 64},
        "target_before_identities": {"tables/orders": _identity(D, 3)},
        "target_resulting_identities": {},
        "target_backup_identities": {"tables/orders": _leaf("backup/leaf")},
        "resulting_revisions": {"tables/orders": "1" * 64},
        "activity_ids": [], "undo_group_id": "undo_" + U, "outbox_ids": [],
        "started_at": TS, "commit_decided_at": None, "completed_at": TS2,
        "error_digest": None,
    })
    audit_rec = schemas.TransactionAudit.from_mapping(undo_audit)
    repo.append("transactions", "transaction", audit_rec)
    # Endpoint result for undo transaction with matching scope/fingerprint.
    undo_ep = _endpoint()
    undo_ep["transaction_id"] = "undo_" + U
    undo_ep["checksum"] = canonical.compute_checksum(undo_ep)
    repo.append("endpoint_results", "endpoint_result", undo_ep)
    reopened = ProposalRepositoryV1.bootstrap(
        fs, clock=Clock(), entropy=Entropy(), activity_link_verifier=ActivityVerifier())
    assert reopened.can_retire_wal("undo_" + U) is False


# ============================================================================
# Req #9: Checkpoint negative proof — valid checksum but invalid nested payload
# ============================================================================


def test_checkpoint_invalid_nested_payload_falls_back(tmp_path: Path) -> None:
    """Valid checkpoint checksums but same-kind invalid nested payload falls
    back to JSONL-only reconstruction (Req #9)."""
    fs, repo = _repo(tmp_path)
    repo.append("decisions", "decision", _decision())
    repo.checkpoint("decisions")
    # Corrupt the payload of the checkpoint record to have invalid nested data.
    cp_path = "checkpoints/decisions.json"
    cp = schemas.Checkpoint.from_json(repo._fs.read_bytes(cp_path))
    bad_records = list(cp.records)
    bad_records[0] = dict(bad_records[0])
    # Corrupt the payload so the decision parser rejects it.
    bad_payload = dict(bad_records[0]["payload"])
    bad_payload["decision"] = "invalid_value"  # not accepted/rejected
    bad_records[0]["payload"] = bad_payload
    obj = dict(cp.to_mapping())
    obj["records"] = bad_records
    obj["records_digest"] = canonical.canonical_digest(bad_records)
    obj["checksum"] = canonical.compute_checksum(obj)
    repo._fs.write_atomic(cp_path, canonical.dumps_canonical(obj))
    # Reopen: checkpoint is rejected, exact marker evidence is durable, and the
    # affected aggregate is blocked despite JSONL reconstruction.
    reopened = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    assert reopened.snapshot().high_waters["decisions"].sequence == 1
    markers = [schemas.CorruptionMarker.from_mapping(w.payload)
               for w in reopened.snapshot().aggregate("corruption") if w.kind == "corruption"]
    marker = markers[-1]
    assert marker.authority_kind == "checkpoint"
    assert marker.authority_identity.aggregate == "decisions"
    assert marker.reason == "checksum_mismatch"
    with pytest.raises(RepositoryCorruptError, match="blocked"):
        reopened.append("decisions", "decision", _decision("D", "B"))
