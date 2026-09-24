from __future__ import annotations

import os
import pytest

from okf_loom.proposal_review.activation import activate_http
from okf_loom.proposal_review.http import dispatch
from okf_loom.server import OKFWikiHandler
from okf_loom.studio import Studio
from test_proposal_http import _Handler, _RealProposalHTTP, _mutation


def test_sec_01_closed_ops() -> None:
    from okf_loom.proposal_review import schemas
    operation = {"schema": 1, "op_id": "op_" + "0" * 26,
        "kind": "execute_command", "target": "tables/orders",
        "args": {"command": "id"}, "reads": ["tables/orders"],
        "writes": ["tables/orders"],
        "expected_revisions": {"tables/orders": "0" * 64},
        "depends_on": [], "extensions": {}}
    with pytest.raises(Exception):
        schemas.Operation.from_mapping(operation)


def test_sec_02_trusted_dirfds(tmp_path) -> None:
    from okf_loom.proposal_review.publication import TrustedPublicationAdapter
    bundle = tmp_path / "bundle"; repository = tmp_path / "repository"
    bundle.mkdir(mode=0o700); repository.mkdir(mode=0o700)
    bundle.chmod(0o700); repository.chmod(0o700)
    target = bundle / "orders.md"; source = bundle / "source.md"
    source.write_bytes(b"before\n"); target.symlink_to(source)
    with TrustedPublicationAdapter(bundle, repository) as publication:
        with pytest.raises(OSError):
            publication.prepare("txn_" + "1" * 26, {"orders": b"after\n"})


def test_sec_03_substitution_matrix(tmp_path) -> None:
    from okf_loom.proposal_review.errors import StorageSafetyError
    from okf_loom.proposal_review.publication import TrustedPublicationAdapter
    bundle = tmp_path / "bundle"; repository = tmp_path / "repository"
    bundle.mkdir(mode=0o700); repository.mkdir(mode=0o700)
    bundle.chmod(0o700); repository.chmod(0o700)
    target = bundle / "orders.md"; target.write_bytes(b"before\n"); target.chmod(0o600)
    with TrustedPublicationAdapter(bundle, repository) as publication:
        prepared = publication.prepare("txn_" + "2" * 26, {"orders": b"after\n"})
        replacement = target.with_suffix(".new"); replacement.write_bytes(b"before\n")
        replacement.chmod(0o600); replacement.replace(target)
        with pytest.raises(StorageSafetyError):
            publication.publish(prepared.transaction_id, prepared.targets[0])


def test_sec_04_resource_caps() -> None:
    from okf_loom.proposal_review.service_contracts import ApplyInput
    from test_proposal_apply import D, U
    with pytest.raises(Exception):
        ApplyInput.from_mapping({"schema": 1, "proposal_id": U, "proposal_revision": 1,
            "actor": "x" * 129, "snapshot_digest": D,
            "validation_context_digest": D, "decision_set_token": D,
            "read_model_token": D})


def test_sec_05_exposure_matrix(tmp_path) -> None:
    live = _RealProposalHTTP(tmp_path)
    try:
        direct, _, _ = live.request("POST", "/__proposal/v1/proposals", {}, "A" * 32)
        assert direct == 422  # both capabilities and real loopback passed exposure gates
        proxied, body, _ = live.request(
            "POST", "/__proposal/v1/proposals", {}, "B" * 32, proposal_token=False)
        assert proxied == 401 and body["error"]["code"] == "invalid_proposal_token"
        live.server.studio_edit = False
        read_only, body, _ = live.request(
            "POST", "/__proposal/v1/proposals", {}, "C" * 32, proposal_token=False)
        assert read_only == 403 and body["error"]["code"] == "read_only"
        live.server.studio_edit = True
        for attribute, value in (("proposal_public_exposure", True),
                                 ("tunnel_url", "https://proxy.example")):
            setattr(live.server, attribute, value)
            exposed, body, _ = live.request(
                "POST", "/__proposal/v1/proposals", {}, "D" * 32)
            assert exposed == 403 and body["error"]["code"] == "proposal_mutation_loopback_only"
            setattr(live.server, attribute, False if attribute == "proposal_public_exposure" else None)
    finally:
        live.close()

    # Accepted-socket endpoint ownership is injected directly because a real
    # loopback TCP client cannot forge kernel local/remote endpoint addresses.
    remote = _mutation()
    remote.connection.getpeername = lambda: ("203.0.113.7", 9000)
    dispatch(remote, "POST")
    assert remote.status == 403

    local_bad = _mutation()
    local_bad.connection.getsockname = lambda: ("0.0.0.0", 8787)
    dispatch(local_bad, "POST")
    assert local_bad.status == 403


def test_sec_06_secret_sinks(tmp_path, capsys) -> None:
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n\nsecret proposal body\n")
    activation = activate_http(tmp_path)
    token = activation.proposal_token
    token_path = tmp_path / ".okf-loom/session/.proposal-http-token"
    info = token_path.stat()
    assert (info.st_mode & 0o777) == 0o600
    assert info.st_uid == os.geteuid() and info.st_nlink == 1

    studio = Studio.for_bundle(tmp_path)
    handler = object.__new__(OKFWikiHandler)
    handler.server = type("Server", (), {"studio": studio, "csrf_token": "html-token",
        "proposal_token": token, "studio_live": True, "studio_edit": True,
        "studio_theme": "auto"})()
    assert token not in handler._studio_bootstrap()

    failed = _Handler("/__proposal/v1/proposals", headers={
        "Host": "localhost", "X-OKF-Token": "html-token",
        "X-OKF-Proposal-Token": "wrong"})
    failed.server.proposal_token = token
    dispatch(failed, "POST")
    error_body = failed.wfile.getvalue().decode()
    assert token not in error_body and "secret proposal body" not in error_body
    assert token not in failed.path

    studio.append_event({"type": "proof", "value": "safe"})
    event_bytes = studio.events_path.read_text()
    assert token not in event_bytes and "secret proposal body" not in event_bytes
    diagnostics = capsys.readouterr()
    assert token not in diagnostics.out + diagnostics.err


def test_sec_07_active_code_separation(tmp_path, monkeypatch) -> None:
    from test_proposal_apply import _ready_transaction
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    invoked = []
    monkeypatch.setattr(os, "system", lambda *args, **kwargs: invoked.append(args) or 1)
    try:
        result = coordinator.apply(request, "K" * 32)
        assert result.status == "applied" and not invoked
    finally:
        publication.close()


def test_sec_08_constraints_passive() -> None:
    from okf_loom.config import StudioConfig
    constraint = {"deny": ["tables/orders"], "command": "never-authority"}
    config = StudioConfig.from_dict({"constraints": constraint})
    assert config.constraints == constraint
