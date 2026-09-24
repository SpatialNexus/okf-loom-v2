from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from okf_loom.proposal_review.cli_adapter import run
from okf_loom.proposal_review.service_contracts import SuccessEnvelope


DIGEST = "sha256:" + "0" * 64
PROPOSAL_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class _Studio:
    def ensure_session(self):
        return None


class _Service:
    def __init__(self):
        self.repository = object()
        self.calls = []
        self.progress = False

    def apply(self, request, *, idempotency_key):
        self.calls.append(("apply", request, idempotency_key))
        return SuccessEnvelope({
            "operation": "apply",
            "status": "transaction_in_progress" if self.progress else "applied",
        })

    def undo(self, request, *, idempotency_key):
        self.calls.append(("undo", request, idempotency_key))
        return SuccessEnvelope({
            "operation": "undo",
            "status": "transaction_in_progress" if self.progress else "undone",
        })

    def close(self):
        return None


def _args(command: str, key: str | None = "K" * 32):
    common = dict(proposal_command=command, bundle=".", proposal_id=PROPOSAL_ID,
                  revision=1, actor="local-user", read_model_token=DIGEST,
                  idempotency_key=key)
    if command == "apply":
        common.update(snapshot_digest=DIGEST, validation_context_digest=DIGEST,
                      decision_set_token=DIGEST)
    else:
        common.update(transaction_id="txn_" + "0" * 26,
                      undo_group_id="undo_" + "0" * 26)
    return SimpleNamespace(**common)


@pytest.mark.parametrize("command", ["apply", "undo"])
def test_cli_transaction_adapter_passes_complete_authority_and_stable_json(
        command, monkeypatch, capsys):
    service = _Service()
    monkeypatch.setattr(
        "okf_loom.proposal_review.cli_adapter.Studio.for_configured_bundle",
        lambda _bundle: _Studio())
    monkeypatch.setattr("okf_loom.proposal_review.cli_adapter.activate",
                        lambda *_args, **_kwargs: service)
    monkeypatch.setattr(
        "okf_loom.proposal_review.cli_adapter.OutboxReconciler.reconcile_pending",
        lambda *_args, **_kwargs: ())

    assert run(_args(command)) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "schema": 1, "data": {"operation": command,
                                "status": "applied" if command == "apply" else "undone"}}
    assert output.err == ""
    assert service.calls[0][0] == command
    assert service.calls[0][1]["proposal_id"] == PROPOSAL_ID
    assert service.calls[0][1]["read_model_token"] == DIGEST
    assert service.calls[0][2] == "K" * 32


@pytest.mark.parametrize("command", ["apply", "undo"])
def test_cli_transaction_adapter_missing_key_is_usage_error_after_activation(
        command, monkeypatch, capsys):
    service = _Service()
    monkeypatch.setattr(
        "okf_loom.proposal_review.cli_adapter.Studio.for_configured_bundle",
        lambda _bundle: _Studio())
    monkeypatch.setattr("okf_loom.proposal_review.cli_adapter.activate",
                        lambda *_args, **_kwargs: service)

    assert run(_args(command, None)) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["error"]["code"] == "invalid_request"
    assert not service.calls


@pytest.mark.parametrize("command", ["apply", "undo"])
def test_cli_transaction_progress_is_json_stable_and_nonterminal(
        command, monkeypatch, capsys):
    service = _Service()
    service.progress = True
    monkeypatch.setattr(
        "okf_loom.proposal_review.cli_adapter.Studio.for_configured_bundle",
        lambda _bundle: _Studio())
    monkeypatch.setattr("okf_loom.proposal_review.cli_adapter.activate",
                        lambda *_args, **_kwargs: service)
    monkeypatch.setattr(
        "okf_loom.proposal_review.cli_adapter.OutboxReconciler.reconcile_pending",
        lambda *_args, **_kwargs: ())

    assert run(_args(command)) == 7
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == json.dumps({
        "schema": 1,
        "data": {"operation": command, "status": "transaction_in_progress"},
    }, sort_keys=True, separators=(",", ":")) + "\n"
