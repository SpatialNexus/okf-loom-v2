from __future__ import annotations

import builtins
import json
import base64
import pytest

from okf_loom.cli import main
from test_proposal_http import _RealProposalHTTP, _SuccessService, _mutation
from okf_loom.proposal_review.http import dispatch
from test_proposal_apply import _ready_transaction
from okf_loom.proposal_review import canonical


def _cli(capsys, argv):
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


@pytest.mark.parametrize("key,valid", [
    ("A" * 31, False), ("A" * 129, False), ("A" * 31 + "!", False),
    (" " * 32, False), ("A" * 31 + "\x01", False), ("é" * 32, False),
    ("V" * 32, True), ("W" * 128, True),
])
def test_idem_13_shared_key_battery(tmp_path, capsys, key, valid) -> None:
    """Service, HTTP, and CLI enforce one structural validator contract."""
    bundle = tmp_path / "bundle"; bundle.mkdir()
    live = _RealProposalHTTP(bundle)
    request_file = tmp_path / "proposal.json"
    request_file.write_text(json.dumps(live.create_body))
    argv = ["proposal", "create", str(bundle), "--mode", "proposal",
            "--file", str(request_file), "--idempotency-key", key, "--format", "json"]
    try:
        if valid:
            service_result = live.service.create(live.create_body, idempotency_key=key).to_mapping()
            cli_code, cli_result, cli_stderr = _cli(capsys, argv)
            assert cli_code == 0 and cli_result == service_result and cli_stderr == ""
        else:
            with pytest.raises(Exception) as service_error:
                live.service.create(live.create_body, idempotency_key=key)
            assert getattr(service_error.value, "code", None) == "invalid_request"
            cli_code, cli_result, _ = _cli(capsys, argv)
            assert cli_code == 2 and cli_result["error"]["code"] == "invalid_request"
        handler = _mutation("/__proposal/v1/proposals", service=_SuccessService(),
                            extra={"Idempotency-Key": key})
        dispatch(handler, "POST")
        assert handler.status == (201 if valid else 422)
        if not valid:
            assert json.loads(handler.wfile.getvalue())["error"]["code"] == "invalid_request"
    finally:
        live.close()


def test_parity_05_cli(tmp_path, capsys, monkeypatch) -> None:
    """HTTP and CLI consume one repository and replay exact stored envelopes."""
    bundle = tmp_path / "bundle"; bundle.mkdir()
    live = _RealProposalHTTP(bundle)
    request_file = tmp_path / "proposal.json"
    request_file.write_text(json.dumps(live.create_body))
    monkeypatch.setattr(builtins, "input", lambda *args: (_ for _ in ()).throw(
        AssertionError("proposal CLI must not prompt")))
    create_argv = ["proposal", "create", str(bundle), "--mode", "proposal",
                   "--file", str(request_file), "--idempotency-key", "A" * 32,
                   "--format", "json"]
    try:
        status, http_created, _ = live.create("A" * 32)
        cli_code, cli_created, stderr = _cli(capsys, create_argv)
        assert status == 201 and cli_code == 0 and cli_created == http_created and stderr == ""
        proposal_id = http_created["data"]["proposal"]["proposal_id"]
        base = f"/__proposal/v1/proposals/{proposal_id}/revisions/1"

        status, http_list, _ = live.request("GET", "/__proposal/v1/proposals?limit=50")
        code, cli_list, _ = _cli(capsys, ["proposal", "list", str(bundle),
                                              "--limit", "50", "--format", "json"])
        assert status == 200 and code == 0
        assert cli_list["data"]["items"] == http_list["data"]["items"]

        status, http_show, _ = live.request("GET", base)
        code, cli_show, _ = _cli(capsys, ["proposal", "show", str(bundle), proposal_id,
                                              "--revision", "1", "--format", "json"])
        assert status == 200 and code == 0
        assert cli_show["data"]["read_model_token"] == http_show["data"]["read_model_token"]

        decision = {"schema": 1, "decision": "accepted", "reason": "",
                    "previous_row_token": None,
                    "read_model_token": http_show["data"]["read_model_token"]}
        status, http_decision, _ = live.request(
            "POST", base + f"/decisions/{live.op_id}", decision, "B" * 32)
        code, cli_decision, _ = _cli(capsys, ["proposal", "decide", str(bundle), proposal_id,
            "--revision", "1", "--op", live.op_id, "--decision", "accepted",
            "--read-model-token", decision["read_model_token"],
            "--idempotency-key", "B" * 32, "--format", "json"])
        assert status == 200 and code == 0 and cli_decision == http_decision

        # Keyless CLI decision is executable and remains non-interactive.
        current_status, current, _ = live.request("GET", base)
        row = http_decision["data"]["decision"]["row_token"]
        code, keyless, _ = _cli(capsys, ["proposal", "decide", str(bundle), proposal_id,
            "--revision", "1", "--op", live.op_id, "--decision", "rejected",
            "--reason", "changed", "--row-token", row,
            "--read-model-token", current["data"]["read_model_token"], "--format", "json"])
        assert current_status == 200 and code == 0 and keyless["data"]["decision"]["decision"] == "rejected"

        _, ready, _ = live.request("GET", base)
        withdrawal = {"schema": 1, "read_model_token": ready["data"]["read_model_token"],
                      "reason": "stop"}
        status, http_withdraw, _ = live.request("POST", base + "/withdraw", withdrawal, "C" * 32)
        code, cli_withdraw, _ = _cli(capsys, ["proposal", "withdraw", str(bundle), proposal_id,
            "--revision", "1", "--read-model-token", withdrawal["read_model_token"],
            "--reason", "stop", "--idempotency-key", "C" * 32, "--format", "json"])
        assert status == 200 and code == 0 and cli_withdraw == http_withdraw

        # Same-key changed intent is the same public conflict classification.
        changed = dict(live.create_body); changed["summary"] = "changed intent"
        status, http_conflict, _ = live.create("A" * 32, changed)
        request_file.write_text(json.dumps(changed))
        code, cli_conflict, stderr = _cli(capsys, create_argv)
        assert status == 409 and code == 5
        for field in ("code", "retryable", "resync_required", "details"):
            assert cli_conflict["error"][field] == http_conflict["error"][field]
        assert "idempotency_conflict" in stderr

        # Legacy and supersede requests are accepted over HTTP and replayed by CLI.
        legacy_path = bundle / ".okf-loom/session/proposals.jsonl"
        legacy_path.write_text(json.dumps({"id": "legacy-parity", "summary": "old",
            "author": "agent", "created_at": "2026-07-15T00:00:00.000Z"}) + "\n")
        legacy_path.chmod(0o600)
        legacy = dict(live.create_body)
        legacy["legacy_identity"] = {"legacy_id": "legacy-parity", "source_generation": 0}
        status, http_legacy, _ = live.create("E" * 32, legacy)
        request_file.write_text(json.dumps(live.create_body))
        code, cli_legacy, _ = _cli(capsys, ["proposal", "create", str(bundle),
            "--mode", "proposal", "--file", str(request_file),
            "--idempotency-key", "E" * 32, "--regenerate-legacy-id", "legacy-parity",
            "--legacy-source-generation", "0", "--format", "json"])
        assert status == 201 and code == 0 and cli_legacy == http_legacy
        fresh_id = http_legacy["data"]["proposal"]["proposal_id"]
        successor_file = dict(live.create_body); successor_file["summary"] = "successor"
        successor = dict(successor_file)
        successor.update(proposal_id=fresh_id, supersedes_revision=1)
        request_file.write_text(json.dumps(successor_file))
        status, superseded, _ = live.create("D" * 32, successor)
        code, cli_superseded, _ = _cli(capsys, ["proposal", "create", str(bundle),
            "--mode", "proposal", "--file", str(request_file),
            "--idempotency-key", "D" * 32, "--supersede", f"{fresh_id}:1",
            "--format", "json"])
        assert status == 201 and code == 0 and cli_superseded == superseded
    finally:
        live.close()


def test_parity_01_bytes(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    proposal = coordinator._proposal(request["proposal_id"], 1)
    preview = repository.get_preview(proposal.preview_ref.preview_digest)
    reviewed = base64.b64decode(preview.concept_changes[0].after_base64)
    try:
        coordinator.apply(request, "K" * 32)
        assert (bundle / "tables/orders.md").read_bytes() == reviewed
    finally: publication.close()


def test_parity_02_diff(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        proposal = coordinator._proposal(request["proposal_id"], 1)
        preview = repository.get_preview(proposal.preview_ref.preview_digest)
        body = preview.diff.to_mapping(); digest = body.pop("digest")
        assert digest == canonical.canonical_digest(body)
    finally: publication.close()


def test_parity_03_validation(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    proposal = coordinator._proposal(request["proposal_id"], 1)
    preview = repository.get_preview(proposal.preview_ref.preview_digest)
    try:
        result = coordinator.apply(request, "K" * 32)
        audit = next(row.payload for row in repository.snapshot().aggregate("transactions")
                     if row.kind == "transaction" and row.payload.get("transaction_id") == result.transaction_id)
        assert result.validation_digest == preview.validation.digest == audit["validation_digest"]
        assert audit["validation_context_digest"] == preview.validation.validation_context_digest
    finally: publication.close()


def test_parity_04_http(tmp_path) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    live = _RealProposalHTTP(bundle)
    try:
        status, envelope, _ = live.create("A" * 32)
        domain = live.activation.service.show(envelope["data"]["proposal"]["proposal_id"], 1)
        assert status == 201
        assert domain.data["read_model_token"] == envelope["data"]["read_model_token"]
    finally: live.close()
