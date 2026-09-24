import pytest
import os
import dataclasses
from pathlib import Path

from okf_loom.proposal_review.errors import SchemaError
from okf_loom.proposal_review.service_contracts import ApplyInput
from okf_loom.proposal_review.ports import LinuxDirFdFilesystem, RuntimeIdentities
from okf_loom.proposal_review.repository import ProposalRepositoryV1
from okf_loom.proposal_review.service import ProposalService, CapturedServiceBundle, PROPOSAL_CAPABILITY
from okf_loom.proposal_review import canonical, schemas
from okf_loom.proposal_review.publication import TrustedPublicationAdapter
from okf_loom.proposal_review.transactions import ProposalTransactionCoordinator
from test_proposal_service import Clock, Entropy, Capture, RUNTIME, Codec, Validator, View

D = "sha256:" + "0" * 64
U = "0" * 26


def test_apply_input_is_closed() -> None:
    value = {"schema": 1, "proposal_id": U, "proposal_revision": 1, "actor": "local",
             "snapshot_digest": D, "validation_context_digest": D,
             "decision_set_token": D, "read_model_token": D}
    assert ApplyInput.from_mapping(value).snapshot_digest == D
    with pytest.raises(SchemaError):
        ApplyInput.from_mapping({**value, "idempotency_key": "not-in-body"})


def test_apply_input_cannot_select_accepted_set() -> None:
    # Durable behavior is coordinator-owned; the public body cannot choose an accepted set.
    assert "accepted_op_ids" not in ApplyInput.__dataclass_fields__


@pytest.mark.parametrize("key", [None, True, "K" * 31, "K" * 129, "contains space",
    "unicode-é" * 4, "slash/not-allowed" * 3, "line\nbreak" * 4, "K" * 31 + "!"])
def test_apply_idempotency_key_invalid_battery(tmp_path: Path, key) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        with pytest.raises(Exception) as rejected:
            coordinator.apply(request, key)
        assert getattr(rejected.value, "code", None) == "invalid_request"
        assert not list((session / "proposal-review-v1/transactions").glob("*/wal.json"))
    finally:
        publication.close()


class _Activity:
    def __init__(self): self.rows = {}
    def append_activity(self, activity_id, payload):
        current = self.rows.get(activity_id)
        if current is not None: assert current == payload
        self.rows[activity_id] = payload
        return payload
    def verify_activity_ids(self, transaction_id, activity_ids):
        return all(item in self.rows and self.rows[item]["detail"]["transaction_id"] == transaction_id
                   for item in activity_ids)


class _Events:
    def append_proposal_event(self, outbox_id, payload): return 1


def _ready_transaction(tmp_path: Path):
    bundle = tmp_path / "bundle"; session = tmp_path / "session"
    bundle.mkdir(mode=0o700); session.mkdir(mode=0o700)
    bundle.chmod(0o700); session.chmod(0o700)
    content = b"---\ntype: Table\n---\n\nOrders\n"
    (bundle / "tables").mkdir(); (bundle / "tables/orders.md").write_bytes(content)
    (bundle / "tables/orders.md").chmod(0o600)
    fd = os.open(session, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    activity = _Activity()
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy(),
                                                activity_link_verifier=activity)
    capture = Capture(content, path=bundle / "tables/orders.md")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    op_id = "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
    revision = capture.snapshot.concepts[0].revision
    created = service.create({"schema": 1, "mode": "proposal", "author": "agent",
        "summary": "Tag orders", "rationale": "Review", "expected_snapshot": capture.snapshot.to_mapping(),
        "operations": [{"schema": 1, "op_id": op_id, "kind": "set_frontmatter",
            "target": "tables/orders", "args": {"key": "tags", "value": ["orders"]},
            "reads": ["tables/orders"], "writes": ["tables/orders"],
            "expected_revisions": {"tables/orders": revision}, "depends_on": [], "extensions": {}}],
        "provenance": [], "extensions": {}}, idempotency_key="A" * 32).data
    proposal_id = created["proposal"]["proposal_id"]
    shown = service.show(proposal_id, 1).data
    service.decide(proposal_id, 1, op_id, {"schema": 1, "decision": "accepted", "reason": "",
        "previous_row_token": None, "read_model_token": shown["read_model_token"]},
        idempotency_key="B" * 32)
    ready = service.show(proposal_id, 1).data
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    coordinator = ProposalTransactionCoordinator(repository, capture, publication, clock=Clock(),
                                                  activity=activity, outbox=_Events())
    request = {"schema": 1, "proposal_id": proposal_id, "proposal_revision": 1, "actor": "local",
        "snapshot_digest": capture.snapshot.snapshot_digest,
        "validation_context_digest": capture.snapshot.validation_context_digest,
        "decision_set_token": ready["decision_set_token"], "read_model_token": ready["read_model_token"]}
    return bundle, session, repository, coordinator, publication, request, activity


class _MultiCapture:
    def __init__(self, contents, root=None):
        concepts = []
        for index, (name, data) in enumerate(sorted(contents.items()), 1):
            if root is None:
                identity = {"parent_device": 1, "parent_inode": 2,
                    "leaf_device": 1, "leaf_inode": index + 2, "ctime_ns": 4,
                    "mode": 0o600, "size": len(data),
                    "digest": canonical.sha256_hex(data)}
            else:
                path = root / f"{name}.md"; leaf, parent = os.stat(path), os.stat(path.parent)
                identity = {"parent_device": parent.st_dev, "parent_inode": parent.st_ino,
                    "leaf_device": leaf.st_dev, "leaf_inode": leaf.st_ino,
                    "ctime_ns": leaf.st_ctime_ns, "mode": leaf.st_mode,
                    "size": leaf.st_size, "digest": canonical.sha256_hex(data)}
            concepts.append({"concept": name, "revision": canonical.sha256_hex(data),
                             "identity": identity})
        body = {"schema": 1, "concepts": concepts, "capabilities": [PROPOSAL_CAPABILITY],
            "config_digest": D, "validation_context_digest": D, "validator_id": RUNTIME,
            "serializer_id": RUNTIME, "operation_runtime_id": RUNTIME}
        self.snapshot = schemas.Snapshot.from_mapping({**body, "captured_at": "2026-07-14T12:34:56.789Z",
            "snapshot_digest": canonical.canonical_digest(body)})
        self.contents = contents
    def capture(self, *, captured_at, proposal_capability_active):
        revisions = {entry.concept: entry.revision for entry in self.snapshot.concepts}
        view = View(self.snapshot.snapshot_digest, D, D, self.snapshot.capabilities, revisions)
        return CapturedServiceBundle(self.snapshot, dict(self.contents), view, Codec(), Validator())


def _ready_two_target_transaction(tmp_path: Path):
    bundle = tmp_path / "bundle"; session = tmp_path / "session"
    bundle.mkdir(); bundle.chmod(0o700); session.mkdir(); session.chmod(0o700)
    contents = {name: f"---\ntype: Table\n---\n\n{name}\n".encode()
                for name in ("tables/customers", "tables/orders")}
    (bundle / "tables").mkdir()
    for name, data in contents.items():
        path = bundle / f"{name}.md"; path.write_bytes(data); path.chmod(0o600)
    fd = os.open(session, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    activity = _Activity(); repository = ProposalRepositoryV1.bootstrap(
        fs, clock=Clock(), entropy=Entropy(), activity_link_verifier=activity)
    capture = _MultiCapture(contents, bundle)
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    operations = []
    for index, entry in enumerate(capture.snapshot.concepts, 1):
        operations.append({"schema": 1, "op_id": f"op_{index:026d}", "kind": "set_frontmatter",
            "target": entry.concept, "args": {"key": "tags", "value": [entry.concept.split('/')[-1]]},
            "reads": [entry.concept], "writes": [entry.concept],
            "expected_revisions": {entry.concept: entry.revision}, "depends_on": [], "extensions": {}})
    created = service.create({"schema": 1, "mode": "proposal", "author": "agent",
        "summary": "Tag both", "rationale": "Review", "expected_snapshot": capture.snapshot.to_mapping(),
        "operations": operations, "provenance": [], "extensions": {}}, idempotency_key="A" * 32).data
    proposal_id = created["proposal"]["proposal_id"]
    for index, operation in enumerate(operations):
        shown = service.show(proposal_id, 1).data
        service.decide(proposal_id, 1, operation["op_id"], {"schema": 1, "decision": "accepted",
            "reason": "", "previous_row_token": None, "read_model_token": shown["read_model_token"]},
            idempotency_key=chr(66 + index) * 32)
    ready = service.show(proposal_id, 1).data
    publication = TrustedPublicationAdapter(bundle, session / "proposal-review-v1")
    coordinator = ProposalTransactionCoordinator(repository, capture, publication, clock=Clock(),
                                                  activity=activity, outbox=_Events())
    request = {"schema": 1, "proposal_id": proposal_id, "proposal_revision": 1, "actor": "local",
        "snapshot_digest": capture.snapshot.snapshot_digest, "validation_context_digest": D,
        "decision_set_token": ready["decision_set_token"], "read_model_token": ready["read_model_token"]}
    return bundle, session, repository, coordinator, publication, request, activity


def test_apply_05_success(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try: result = coordinator.apply(request, "K" * 32)
    finally: publication.close()
    assert result.status == "applied"
    assert b"tags: ['orders']" in (bundle / "tables/orders.md").read_bytes()
    transaction = repository.read_wal(result.transaction_id)
    assert transaction.phase == "committed"
    intent = repository.read_transaction_intent(result.transaction_id)
    assert transaction.transaction_intent_digest == intent.canonical_digest()
    assert activity.rows
    assert repository.can_retire_wal(result.transaction_id)


def _assert_no_content_transaction(session: Path, original: bytes) -> None:
    root = session / "proposal-review-v1/transactions"
    assert not list(root.glob("*/wal.json"))


def test_apply_01_empty_set(tmp_path: Path, monkeypatch) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    monkeypatch.setattr("okf_loom.proposal_review.transactions.projectors.accepted_apply_set", lambda states: ())
    try:
        with pytest.raises(Exception) as error: coordinator.apply(request, "K" * 32)
        assert getattr(error.value, "code", None) == "invalid_state"
        _assert_no_content_transaction(session, b"")
    finally: publication.close()


def test_apply_02_dependency_closed(tmp_path: Path, monkeypatch) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_two_target_transaction(tmp_path)
    proposal = coordinator._proposal(request["proposal_id"], 1)
    first, operation = proposal.operations
    object.__setattr__(operation, "depends_on", (first.op_id,))
    monkeypatch.setattr(coordinator, "_proposal", lambda proposal_id, revision: proposal)
    monkeypatch.setattr("okf_loom.proposal_review.transactions.projectors.accepted_apply_set",
                        lambda states: (operation.op_id,))
    from okf_loom.proposal_review import transactions
    original_model = transactions.build_read_model
    monkeypatch.setattr(transactions, "build_read_model", lambda inputs: dataclasses.replace(
        original_model(inputs), read_model_token=request["read_model_token"],
        decision_set_token=request["decision_set_token"]))
    try:
        with pytest.raises(Exception) as error: coordinator.apply(request, "K" * 32)
        assert getattr(error.value, "code", None) == "proposal_conflict"
        _assert_no_content_transaction(session, b"")
    finally: publication.close()


def test_dep_07_effective_accepted_authority(tmp_path: Path, monkeypatch) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_two_target_transaction(tmp_path)
    proposal = coordinator._proposal(request["proposal_id"], 1)
    effective = proposal.operations[0].op_id
    monkeypatch.setattr("okf_loom.proposal_review.transactions.projectors.accepted_apply_set",
                        lambda states: (effective,))
    try:
        result = coordinator.apply(request, "K" * 32)
        wal = repository.read_wal(result.transaction_id)
        intent = repository.read_transaction_intent(result.transaction_id)
        audit = next(schemas.TransactionAudit.from_mapping(row.payload)
                     for row in repository.snapshot().aggregate("transactions")
                     if row.kind == "transaction"
                     and row.payload.get("transaction_id") == result.transaction_id)
        assert wal.accepted_op_ids == (effective,)
        assert intent.value["accepted_op_ids"] == [effective]
        assert audit.accepted_op_ids == (effective,)
        assert len(audit.decision_record_ids) == 2
        target = proposal.operations[0].target
        excluded = proposal.operations[1].target
        assert [row.concept for row in wal.targets] == [target]
        assert [row["concept"] for row in intent.value["targets"]] == [target]
        assert b"tags:" in (bundle / f"{target}.md").read_bytes()
        assert b"tags:" not in (bundle / f"{excluded}.md").read_bytes()
    finally:
        publication.close()


def test_apply_03_validation(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    original_capture = coordinator.capture_port
    class FailedValidator:
        def installed_profiles(self): return ("producer",)
        def validate(self, snapshot, *, profile):
            from okf_loom.proposal_review.ports import FrozenValidationReport
            return FrozenValidationReport((), False)
    class FailedCapture:
        def capture(self, **kwargs):
            value = original_capture.capture(**kwargs)
            return CapturedServiceBundle(value.snapshot, value.concept_bytes, value.current_bundle,
                                         value.codec, FailedValidator())
    coordinator.capture_port = FailedCapture()
    try:
        with pytest.raises(Exception) as error: coordinator.apply(request, "K" * 32)
        assert getattr(error.value, "code", None) == "validation_failed"
        _assert_no_content_transaction(session, b"")
    finally: publication.close()


def test_apply_03a_validation_context(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    original_capture = coordinator.capture_port
    class DriftCapture:
        def capture(self, **kwargs):
            value = original_capture.capture(**kwargs)
            changed = value.snapshot.to_mapping(); changed["validation_context_digest"] = "sha256:" + "1" * 64
            changed["snapshot_digest"] = canonical.canonical_digest({k: v for k, v in changed.items()
                                                                      if k not in {"captured_at", "snapshot_digest"}})
            snapshot = schemas.Snapshot.from_mapping(changed)
            return CapturedServiceBundle(snapshot, value.concept_bytes, value.current_bundle,
                                         value.codec, value.validator)
    coordinator.capture_port = DriftCapture()
    try:
        with pytest.raises(Exception) as error: coordinator.apply(request, "K" * 32)
        assert getattr(error.value, "code", None) == "proposal_conflict"
        assert error.value.details == [{"schema": 1, "code": "validation_stale",
            "proposal_id": request["proposal_id"], "proposal_revision": 1,
            "op_id": None, "concept": None,
            "expected": request["validation_context_digest"],
            "actual": "sha256:" + "1" * 64,
            "message": "Validation context differs from the reviewed authority.",
            "detected_at": "2026-07-14T12:34:56.789Z"}]
        _assert_no_content_transaction(session, b"")
    finally: publication.close()


@pytest.mark.parametrize("identity_field", ["operation_runtime_id", "validator_id", "serializer_id"])
def test_runtime_identity_drift_requires_fresh_review_before_apply(tmp_path: Path, identity_field: str) -> None:
    from okf_loom.proposal_review import projectors
    from okf_loom.proposal_review.errors import RepositoryError
    from okf_loom.proposal_snapshot import DEFAULT_RUNTIME_IDENTITIES

    current_identity = getattr(DEFAULT_RUNTIME_IDENTITIES, identity_field)

    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    original_capture = coordinator.capture_port
    path = bundle / "tables/orders.md"
    original = path.read_bytes()
    proposal = coordinator._proposal(request["proposal_id"], 1)
    now = Clock().now()

    class DriftCapture:
        def capture(self, **kwargs):
            value = original_capture.capture(**kwargs)
            changed = value.snapshot.to_mapping()
            changed[identity_field] = current_identity
            changed["snapshot_digest"] = canonical.canonical_digest({
                k: v for k, v in changed.items() if k not in {"captured_at", "snapshot_digest"}
            })
            snapshot = schemas.Snapshot.from_mapping(changed)
            current = dataclasses.replace(value.current_bundle,
                snapshot_digest=snapshot.snapshot_digest, **{identity_field: current_identity})
            return CapturedServiceBundle(snapshot, value.concept_bytes, current,
                                         value.codec, value.validator)

    try:
        before = original_capture.capture(captured_at=now, proposal_capability_active=True)
        assert projectors.is_preview_fresh(proposal, before.current_bundle, now=now)
        coordinator.capture_port = DriftCapture()
        after = coordinator.capture_port.capture(captured_at=now, proposal_capability_active=True)
        assert after.concept_bytes == before.concept_bytes
        assert after.snapshot.validation_context_digest == before.snapshot.validation_context_digest
        assert not projectors.is_preview_fresh(proposal, after.current_bundle, now=now)
        conflicts = projectors.project_conflicts(proposal, after.current_bundle, now=now)
        assert any(row["code"] == "validation_stale"
                   and row["expected"] == getattr(proposal.expected_snapshot, identity_field)
                   and row["actual"] == current_identity for row in conflicts.conflicts)
        with pytest.raises(RepositoryError, match="runtime authority differs") as error:
            coordinator.apply(request, "K" * 32)
        assert error.value.code == "proposal_conflict"
        assert path.read_bytes() == original
        _assert_no_content_transaction(session, original)
        assert coordinator._proposal(request["proposal_id"], 1) == proposal
    finally:
        publication.close()


def test_apply_03b_same_bytes_new_inode(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    changed = False
    def substitute(boundary, evidence):
        nonlocal changed
        if boundary == "before_rename" and not changed:
            changed = True
            path = bundle / "tables/orders.md"; replacement = path.with_suffix(".new")
            replacement.write_bytes(path.read_bytes()); replacement.chmod(0o600)
            replacement.replace(path)
    publication._fault_injector = substitute
    try:
        with pytest.raises(Exception) as blocked:
            coordinator.apply(request, "K" * 32)
        assert getattr(blocked.value, "code", None) == "indeterminate_outcome"
        transaction_id = next(row.payload["transaction_id"]
            for row in repository.snapshot().aggregate("endpoint_results")
            if row.kind == "endpoint_result" and row.payload.get("state") == "indeterminate")
        assert repository.read_wal(transaction_id).phase == "indeterminate"
        endpoint_rows = [row.payload for row in repository.snapshot().aggregate("endpoint_results")
                         if row.kind == "endpoint_result"
                         and row.payload.get("transaction_id") == transaction_id]
        prepared = next(row for row in endpoint_rows if row["state"] == "prepared")
        indeterminate = next(row for row in endpoint_rows if row["state"] == "indeterminate")
        assert (prepared["record_id"], prepared["expires_at"]) == (
            indeterminate["record_id"], indeterminate["expires_at"])
        with pytest.raises(Exception) as replayed:
            coordinator.apply(request, "K" * 32)
        assert getattr(replayed.value, "code", None) == "indeterminate_outcome"
        assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()
    finally:
        publication.close()


def test_apply_reviewed_target_identity_change_has_zero_authority(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    path = bundle / "tables/orders.md"; original = path.read_bytes()
    replacement = path.with_suffix(".new"); replacement.write_bytes(original)
    replacement.chmod(0o600); replacement.replace(path)
    coordinator.capture_port = Capture(original, path=path)
    try:
        with pytest.raises(Exception) as blocked:
            coordinator.apply(request, "K" * 32)
        assert getattr(blocked.value, "code", None) == "proposal_conflict"
        assert len(blocked.value.details) == 1
        assert blocked.value.details[0]["code"] == "target_identity_changed"
        assert blocked.value.details[0]["concept"] == "tables/orders"
        assert not list((session / "proposal-review-v1/transactions").glob("*/wal.json"))
        assert not [row for row in repository.snapshot().aggregate("endpoint_results")
                    if row.kind == "endpoint_result"
                    and row.payload.get("transaction_id") is not None]
    finally:
        publication.close()


def test_apply_before_exchange_syscall_substitution_is_indeterminate(tmp_path: Path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    changed = False
    def substitute(boundary, evidence):
        nonlocal changed
        if boundary == "before_exchange_syscall" and not changed:
            changed = True
            transaction_id = evidence["transaction_id"]
            stage = session / f"proposal-review-v1/transactions/{transaction_id}/stage/000000.bin"
            replacement = stage.with_suffix(".new")
            replacement.write_bytes(stage.read_bytes()); replacement.chmod(0o600)
            replacement.replace(stage)
    publication._fault_injector = substitute
    try:
        with pytest.raises(Exception) as blocked:
            coordinator.apply(request, "K" * 32)
        assert getattr(blocked.value, "code", None) == "indeterminate_outcome"
        transaction_id = next(row.payload["transaction_id"]
            for row in repository.snapshot().aggregate("endpoint_results")
            if row.kind == "endpoint_result" and row.payload.get("state") == "indeterminate")
        assert repository.read_wal(transaction_id).phase == "indeterminate"
    finally:
        publication.close()


def test_apply_04_parity(tmp_path: Path, monkeypatch) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    from okf_loom.proposal_review import transactions
    original = transactions.stage_operations
    def drift(*args, **kwargs):
        staged = original(*args, **kwargs)
        after = dict(staged.after); key = next(iter(after)); after[key] += b"drift"
        return type(staged)(staged.operation_order, staged.before, after, staged.contributors)
    monkeypatch.setattr(transactions, "stage_operations", drift)
    try:
        with pytest.raises(Exception) as error: coordinator.apply(request, "K" * 32)
        assert getattr(error.value, "code", None) == "contract_parity_failure"
        _assert_no_content_transaction(session, b"")
    finally: publication.close()


def test_apply_06_pre_wal(tmp_path: Path) -> None:
    from test_proposal_crash import _killed
    for index, boundary in enumerate(("transaction_allocated", "private_files_fsynced",
                                      "intent_published", "history_published")):
        root = tmp_path / str(index); root.mkdir()
        bundle, session, repository, coordinator, publication, request = _killed(root, boundary)
        publication.close()
        assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()
        assert not list((session / "proposal-review-v1/transactions").glob("*/wal.json"))


def test_apply_07_pre_decision(tmp_path: Path) -> None:
    from test_proposal_crash import _recover
    bundle, repository = _recover(tmp_path, "wal_prepared")
    assert b"tags:" not in (bundle / "tables/orders.md").read_bytes()


def test_apply_07a_post_decision(tmp_path: Path) -> None:
    from test_proposal_crash import _recover
    bundle, repository = _recover(tmp_path, "commit_decided")
    assert b"tags:" in (bundle / "tables/orders.md").read_bytes()


def test_apply_08_foreign_bytes(tmp_path: Path) -> None:
    from test_proposal_crash import _killed, _recover_existing
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    publication.close(); transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
    stage = next((session / f"proposal-review-v1/transactions/{transaction_id}/stage").iterdir())
    stage.unlink(); stage.write_bytes(b"substituted"); stage.chmod(0o600)
    _, reopened = _recover_existing(bundle, session, indeterminate=True)
    assert reopened.read_wal(transaction_id).phase == "indeterminate"
