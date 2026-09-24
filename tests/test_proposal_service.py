"""Bundle 3 service contract and import-boundary proofs."""
from __future__ import annotations

import ast
import os
import threading
import time
from types import SimpleNamespace
from dataclasses import dataclass
from pathlib import Path

import pytest

from okf_loom.proposal_review.errors import ProposalReviewError, RepositoryError, SchemaError
from okf_loom.proposal_review import canonical, schemas
from okf_loom.proposal_review.ports import (DecodedDocument, FrozenValidationReport,
                                            LinuxDirFdFilesystem, RuntimeIdentities)
from okf_loom.proposal_review.repository import ProposalRepositoryV1
from okf_loom.proposal_review.service import (PROPOSAL_CAPABILITY, CapturedServiceBundle,
                                              ProposalService)
from okf_loom.proposal_review.service_contracts import (DecisionInput, StrictListQuery,
                                                        WithdrawalInput)

D = "sha256:" + "0" * 64
TS = "2026-07-14T12:34:56.789Z"
RUNTIME = "runtime@1.0+sha256:" + "0" * 64


def test_service_inputs_are_closed_and_do_not_accept_persisted_fields() -> None:
    DecisionInput.from_mapping({"schema": 1, "decision": "accepted", "reason": "",
                                "previous_row_token": None, "read_model_token": D})
    with pytest.raises(SchemaError):
        DecisionInput.from_mapping({"schema": 1, "decision": "accepted", "reason": "",
                                    "previous_row_token": None, "read_model_token": D,
                                    "record_id": "caller-owned"})
    WithdrawalInput.from_mapping({"schema": 1, "read_model_token": D, "reason": "stop"})


@pytest.mark.parametrize("query", [
    {"limit": True}, {"limit": 0}, {"limit": 101}, {"state": "draft"},
    {"cursor": ""}, {"unknown": "x"},
])
def test_list_query_fails_closed(query) -> None:
    with pytest.raises(ProposalReviewError):
        StrictListQuery.from_mapping(query)


def test_service_exposes_transport_neutral_apply_and_undo_only() -> None:
    assert hasattr(ProposalService, "apply")
    assert hasattr(ProposalService, "undo")
    transport_adapters = {"activation.py", "http.py", "cli_adapter.py", "studio_outbox.py"}
    for path in (Path(__file__).parents[1] / "scripts/okf_loom/proposal_review").glob("*.py"):
        if path.name in transport_adapters:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported = ast.unparse(node)
                assert not any(name in imported for name in ("server", "cli", "render", "viewer"))


def test_service_09_capability_gate(tmp_path) -> None:
    root = tmp_path / "inactive"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    before = repository.snapshot().token
    class NoCapture:
        def capture(self, **kwargs): raise AssertionError("inactive service captured bundle")
    service = ProposalService(repository, NoCapture(), Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME))
    calls = [
        lambda: service.create(b"not-json", idempotency_key="A" * 32),
        lambda: service.list({}), lambda: service.show("bad", 1),
        lambda: service.decide("bad", 1, "bad", {}, idempotency_key="B" * 32),
        lambda: service.withdraw("bad", 1, {}, idempotency_key="C" * 32),
        lambda: service.apply(b"not-json", idempotency_key="D" * 32),
        lambda: service.undo(b"not-json", idempotency_key="E" * 32),
    ]
    for call in calls:
        with pytest.raises(RepositoryError) as caught:
            call()
        assert caught.value.code == "capability_unavailable"
    assert repository.snapshot().token == before
    assert repository._fs.list_dir("service-wal") == ()


class Clock:
    def now(self): return TS


class Entropy:
    def __init__(self): self.n = 0
    def token_bytes(self, count): self.n += 1; return self.n.to_bytes(count, "big")


class Codec:
    def decode(self, data): return DecodedDocument({"type": "Table"}, data.decode().split("\n\n", 1)[-1])
    def encode(self, original, document):
        tags = document.frontmatter.get("tags", [])
        return ("---\ntype: Table\ntags: " + str(tags) + "\n---\n\n" + document.body).encode()


class Validator:
    def installed_profiles(self): return ("producer",)
    def validate(self, snapshot, *, profile): return FrozenValidationReport((), True)


@dataclass(frozen=True)
class View:
    snapshot_digest: str; config_digest: str; validation_context_digest: str
    capabilities: tuple[str, ...]; concept_revisions: dict[str, str]
    validator_id: str = RUNTIME; serializer_id: str = RUNTIME; operation_runtime_id: str = RUNTIME
    def environment_digest(self): return canonical.canonical_digest({"snapshot": self.snapshot_digest})


class Capture:
    def __init__(self, content, capabilities=(PROPOSAL_CAPABILITY,), path=None):
        revision = canonical.sha256_hex(content)
        if path is None:
            identity = {"parent_device": 1, "parent_inode": 2, "leaf_device": 1,
                        "leaf_inode": 3, "ctime_ns": 4, "mode": 0o600,
                        "size": len(content), "digest": revision}
        else:
            leaf, parent = os.stat(path, follow_symlinks=False), os.stat(path.parent)
            identity = {"parent_device": parent.st_dev, "parent_inode": parent.st_ino,
                        "leaf_device": leaf.st_dev, "leaf_inode": leaf.st_ino,
                        "ctime_ns": leaf.st_ctime_ns, "mode": leaf.st_mode,
                        "size": leaf.st_size, "digest": revision}
        body = {"schema": 1, "concepts": [{"concept": "tables/orders", "revision": revision,
                                             "identity": identity}],
                "capabilities": list(capabilities), "config_digest": D, "validation_context_digest": D,
                "validator_id": RUNTIME, "serializer_id": RUNTIME, "operation_runtime_id": RUNTIME}
        self.snapshot = schemas.Snapshot.from_mapping({**body, "captured_at": TS,
                                                       "snapshot_digest": canonical.canonical_digest(body)})
        self.content = content
    def capture(self, *, captured_at, proposal_capability_active):
        view = View(self.snapshot.snapshot_digest, D, D, self.snapshot.capabilities,
                    {"tables/orders": self.snapshot.concepts[0].revision})
        return CapturedServiceBundle(self.snapshot, {"tables/orders": self.content},
                                     view, Codec(), Validator())


def test_create_show_decide_withdraw_durable_service_slice(tmp_path) -> None:
    root = tmp_path / "session"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    revision = capture.snapshot.concepts[0].revision
    op_id = "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
    created = service.create({
        "schema": 1, "mode": "proposal", "author": "agent", "summary": "Tag orders",
        "rationale": "Review the metadata change.", "expected_snapshot": capture.snapshot.to_mapping(),
        "operations": [{"schema": 1, "op_id": op_id, "kind": "set_frontmatter",
            "target": "tables/orders", "args": {"key": "tags", "value": ["orders"]},
            "reads": ["tables/orders"], "writes": ["tables/orders"],
            "expected_revisions": {"tables/orders": revision}, "depends_on": [], "extensions": {}}],
        "provenance": [], "extensions": {},
    }, idempotency_key="A" * 32).data
    pid = created["proposal"]["proposal_id"]
    shown = service.show(pid, 1).data
    assert shown["state"] == "in_review" and shown["read_model_token"] == created["read_model_token"]
    assert repository.pending_outbox()[-1].payload["read_model_token"] == shown["read_model_token"]
    decided = service.decide(pid, 1, op_id, {
        "schema": 1, "decision": "accepted", "reason": "", "previous_row_token": None,
        "read_model_token": shown["read_model_token"]}, idempotency_key="B" * 32).data
    ready = service.show(pid, 1).data
    assert decided["read_model_token"] == ready["read_model_token"] and ready["state"] == "ready"
    assert repository.pending_outbox()[-1].payload["read_model_token"] == ready["read_model_token"]
    withdrawn = service.withdraw(pid, 1, {"schema": 1, "read_model_token": ready["read_model_token"],
                                           "reason": "No longer needed"},
                                   idempotency_key="C" * 32).data
    assert withdrawn["state"] == "withdrawn"
    assert service.show(pid, 1).data["read_model_token"] == withdrawn["read_model_token"]
    assert repository.pending_outbox()[-1].payload["read_model_token"] == withdrawn["read_model_token"]


def test_create_token_bucket_burst_with_fake_clock(tmp_path) -> None:
    root = tmp_path / "rate"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    revision = capture.snapshot.concepts[0].revision
    def request(index):
        return {"schema": 1, "mode": "proposal", "author": "agent", "summary": f"Tag {index}",
            "rationale": "Exercise deterministic burst admission.",
            "expected_snapshot": capture.snapshot.to_mapping(),
            "operations": [{"schema": 1, "op_id": f"op_{index:026d}",
                "kind": "set_frontmatter", "target": "tables/orders",
                "args": {"key": "tags", "value": [str(index)]},
                "reads": ["tables/orders"], "writes": ["tables/orders"],
                "expected_revisions": {"tables/orders": revision},
                "depends_on": [], "extensions": {}}], "provenance": [], "extensions": {}}
    for index, key in enumerate(("D", "E", "F"), 1):
        service.create(request(index), idempotency_key=key * 32)
    with pytest.raises(RepositoryError) as caught:
        service.create(request(4), idempotency_key="G" * 32)
    assert caught.value.code == "create_rate_limited"


def test_list_cursor_worker_handoff_is_stateless_and_preview_free(tmp_path) -> None:
    root = tmp_path / "pagination"; root.mkdir(mode=0o700); os.chmod(root, 0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: fs = LinuxDirFdFilesystem.adopt(fd)
    finally: os.close(fd)
    repository = ProposalRepositoryV1.bootstrap(fs, clock=Clock(), entropy=Entropy())
    capture = Capture(b"---\ntype: Table\n---\n\nOrders\n")
    service = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    revision = capture.snapshot.concepts[0].revision
    for index, key in enumerate(("H", "I", "J"), 1):
        service.create({"schema": 1, "mode": "proposal", "author": "agent",
            "summary": f"Proposal {index}", "rationale": "Exercise cursor handoff.",
            "expected_snapshot": capture.snapshot.to_mapping(),
            "operations": [{"schema": 1, "op_id": f"op_{index:026d}",
                "kind": "set_frontmatter", "target": "tables/orders",
                "args": {"key": "tags", "value": [str(index)]},
                "reads": ["tables/orders"], "writes": ["tables/orders"],
                "expected_revisions": {"tables/orders": revision}, "depends_on": [],
                "extensions": {}}], "provenance": [], "extensions": {}},
            idempotency_key=key * 32)
    page1 = service.list({"limit": 1}).data
    assert page1["next_cursor"]
    repository.get_preview = lambda digest: (_ for _ in ()).throw(
        AssertionError("list opened preview authority"))
    other_worker = ProposalService(repository, capture, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                                   proposal_capability_active=True)
    page2 = other_worker.list({"limit": 1, "cursor": page1["next_cursor"]}).data
    assert page2["items"] and page2["items"][0]["proposal_id"] != page1["items"][0]["proposal_id"]


class _DeadlineRepository:
    def __init__(self, *, admitted: bool):
        self.admitted = admitted; self.started = threading.Event(); self.finish = threading.Event()
        self.retained = None
    def begin_service_intent(self, intent, precondition, deadline):
        self.started.set()
        if self.admitted: self.retained = intent
        self.finish.wait(2)
        if not self.admitted and time.monotonic() >= deadline:
            raise RepositoryError("deadline", code="operation_timeout")
        self.retained = intent
        return intent
    def observe_service_intent_nonblocking(self, scope, key_digest, fingerprint):
        return True, self.retained


@pytest.mark.parametrize("admitted,ambiguous,expected", [
    (False, False, "operation_timeout"),
    (True, False, "service_mutation_in_progress"),
    (False, True, "indeterminate_outcome"),
])
def test_service_absolute_mutation_timeout_is_nonblocking(admitted, ambiguous, expected):
    repository = _DeadlineRepository(admitted=admitted)
    service = ProposalService(repository, None, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    intent = SimpleNamespace(scope="scope", key_digest=D,
                             request_fingerprint=D, terminal_response={"schema": 1})
    started = time.monotonic()
    with pytest.raises(RepositoryError) as caught:
        service._admit_intent_with_deadline(intent, None, started + .02,
                                            ambiguous_on_timeout=ambiguous)
    assert caught.value.code == expected and time.monotonic() - started < .15
    repository.finish.set()


def test_service_same_key_retry_after_late_admission_is_exact():
    repository = _DeadlineRepository(admitted=True)
    service = ProposalService(repository, None, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    intent = SimpleNamespace(scope="scope", key_digest=D, request_fingerprint=D,
                             terminal_response={"schema": 1, "data": {"ok": True}})
    with pytest.raises(RepositoryError) as caught:
        service._admit_intent_with_deadline(intent, None, time.monotonic() + .02)
    assert caught.value.code == "service_mutation_in_progress"
    repository.finish.set()
    replay = service._admit_intent_with_deadline(intent, None, time.monotonic() + 1)
    assert replay.terminal_response == intent.terminal_response


@pytest.mark.parametrize("late_error", [False, True])
def test_service_late_read_completion_never_blocks_timeout(late_error):
    service = ProposalService(None, None, Clock(), RuntimeIdentities(RUNTIME, RUNTIME, RUNTIME),
                              proposal_capability_active=True)
    finish = threading.Event()
    def work():
        finish.wait(2)
        if late_error: raise RuntimeError("late read failure")
        return "late read success"
    started = time.monotonic()
    with pytest.raises(RepositoryError) as caught:
        service._run_read_job(work, timeout_seconds=.02)
    assert caught.value.code == "operation_timeout" and time.monotonic() - started < .15
    finish.set()
