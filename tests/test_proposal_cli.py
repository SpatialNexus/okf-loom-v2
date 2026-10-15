from __future__ import annotations

import pytest
import json

from okf_loom.cli import _normalize_proposal_value_argv, build_parser, main
from conftest import private_session_root
from okf_loom.proposal_review.activation import ProductionCapturePort
from okf_loom.proposal_review.activation import activate
from okf_loom.proposal_review.studio_outbox import StudioOutboxAppender
from okf_loom.proposal_review.cli_adapter import _exit
from okf_loom.proposal_review.service_contracts import (TRANSPORT_CODES,
                                                        CLI_NONTERMINAL,
                                                        success_cli_exit,
                                                        transport_disposition)
from okf_loom.proposal_review.service_contracts import StrictListQuery
from okf_loom.proposal_review import ids


def test_cli_requires_explicit_revision_and_registers_apply_and_undo() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["proposal", "show", ".", "proposal-id"])
    parsed = parser.parse_args([
        "proposal", "show", ".", "proposal-id", "--revision", "2", "--format", "json"])
    assert parsed.revision == 2 and parsed.format == "json"
    apply = parser.parse_args([
        "proposal", "apply", ".", "proposal-id", "--revision", "1",
        "--actor", "local-user", "--snapshot-digest", "snapshot",
        "--validation-context-digest", "validation", "--decision-set-token", "decisions",
        "--read-model-token", "read", "--idempotency-key", "K" * 32,
    ])
    assert apply.proposal_command == "apply"
    assert apply.snapshot_digest == "snapshot"
    undo = parser.parse_args([
        "proposal", "undo", ".", "proposal-id", "--revision", "1",
        "--actor", "local-user", "--transaction-id", "txn_" + "0" * 26,
        "--undo-group-id", "undo_" + "0" * 26, "--read-model-token", "read",
        "--idempotency-key", "U" * 32,
    ])
    assert undo.proposal_command == "undo"
    assert undo.transaction_id.startswith("txn_")


def test_legacy_regeneration_flags_are_registered_as_a_pair() -> None:
    parser = build_parser()
    parsed = parser.parse_args(["proposal", "create", ".", "--mode", "proposal",
                                "--file", "x", "--idempotency-key", "k",
                                "--regenerate-legacy-id", "old",
                                "--legacy-source-generation", "3"])
    assert parsed.regenerate_legacy_id == "old" and parsed.legacy_source_generation == 3


@pytest.mark.parametrize("option,value", [
    ("--row-token", "-" + "A" * 42),
    ("--cursor", "-cursor-token"),
    ("--idempotency-key", "-" + "k" * 31),
])
@pytest.mark.parametrize("equals", [False, True])
def test_proposal_dash_leading_values_support_space_and_equals_forms(
        option: str, value: str, equals: bool, monkeypatch) -> None:
    captured = {}

    def fake_run(args):
        captured.update(vars(args))
        return 0

    monkeypatch.setattr("okf_loom.proposal_review.cli_adapter.run", fake_run)
    argv = ["proposal", "list", ".", "--cursor", "placeholder"]
    # Keep each option on a command where it is registered, while exercising
    # the real main() normalization immediately before parser dispatch.
    if option == "--cursor":
        argv = ["proposal", "list", "."]
    elif option in {"--row-token", "--idempotency-key"}:
        argv = ["proposal", "decide", ".", "proposal-id", "--revision", "1",
                "--op", "op-id", "--decision", "accepted",
                "--read-model-token", "read-token"]
    argv.extend([option + "=" + value] if equals else [option, value])
    main(argv)
    assert captured[option[2:].replace("-", "_")] == value


def test_dash_leading_required_values_match_direct_validators() -> None:
    row_token = "-" + "A" * 42
    key = "-" + "k" * 31
    assert ids.validate_row_token(row_token) == row_token
    assert ids.validate_idempotency_key(key) == key
    assert StrictListQuery.from_mapping({"cursor": "-cursor-token"}).cursor == "-cursor-token"


def test_normalizer_is_narrow_and_does_not_rewrite_non_allowlisted_flags() -> None:
    argv = ["proposal", "list", ".", "--state", "-ready", "--unknown", "-value"]
    assert _normalize_proposal_value_argv(argv) == argv


def test_non_proposal_dash_value_is_not_rewritten_and_argparse_rejects() -> None:
    argv = ["comment-claim", ".", "comment-1", "--actor", "--format=json"]
    assert _normalize_proposal_value_argv(argv) == argv
    with pytest.raises(SystemExit):
        build_parser().parse_args(_normalize_proposal_value_argv(argv))


def test_other_proposal_dash_value_is_not_rewritten_and_argparse_rejects() -> None:
    argv = ["proposal", "decide", ".", "proposal-1", "--revision", "1",
            "--op", "op-1", "--decision", "accepted", "--read-model-token",
            "read-token", "--actor", "--format=json"]
    assert _normalize_proposal_value_argv(argv) == argv
    with pytest.raises(SystemExit):
        build_parser().parse_args(_normalize_proposal_value_argv(argv))


def test_repeated_proposal_cursor_dash_values_use_argparse_last_value(monkeypatch) -> None:
    captured = {}

    def fake_run(args):
        captured.update(vars(args))
        return 0

    monkeypatch.setattr("okf_loom.proposal_review.cli_adapter.run", fake_run)
    argv = ["proposal", "list", ".", "--cursor", "-first",
            "--cursor", "-second"]
    assert main(argv) == 0
    assert captured["cursor"] == "-second"


def test_shared_transport_disposition_owns_cli_exit_and_retry_policy() -> None:
    assert TRANSPORT_CODES
    for code in TRANSPORT_CODES:
        disposition = transport_disposition(code)
        assert _exit(code) == disposition.cli_exit
        assert disposition.retryable is (disposition.http_status in {429, 503})
    for code in ("cursor_expired", "snapshot_changed", "snapshot_unstable"):
        disposition = transport_disposition(code)
        assert disposition.cli_exit == 5 and disposition.resync_required


def test_success_cli_disposition_reserves_zero_for_terminal_results() -> None:
    assert CLI_NONTERMINAL == 7
    assert success_cli_exit({"status": "applied"}) == 0
    assert success_cli_exit({"status": "undone"}) == 0
    assert success_cli_exit({"status": "transaction_in_progress"}) == CLI_NONTERMINAL


def test_cli_executes_create_list_show_decide_withdraw_json_only(
        tmp_path, capsys, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    private_session_root(bundle)
    (bundle / "orders.md").write_text(
        "---\ntype: Table\ntitle: Orders\ndescription: Orders table.\n"
        "tags: [commerce]\ntimestamp: 2026-07-15T00:00:00Z\nresource: /orders.md\n"
        "---\n\n# Orders\n")
    (bundle / "orders.md").chmod(0o600)
    captured = ProductionCapturePort(bundle).capture(
        captured_at="2026-07-15T00:00:00.000Z", proposal_capability_active=True)
    revision = captured.snapshot.concepts[0].revision
    op_id = "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
    request = {"schema": 1, "mode": "proposal", "author": "agent",
        "summary": "Tag orders", "rationale": "Review metadata.",
        "expected_snapshot": captured.snapshot.to_mapping(),
        "operations": [{"schema": 1, "op_id": op_id, "kind": "set_frontmatter",
            "target": "orders", "args": {"key": "tags", "value": ["orders"]},
            "reads": ["orders"], "writes": ["orders"],
            "expected_revisions": {"orders": revision}, "depends_on": [],
            "extensions": {}}], "provenance": [], "extensions": {}}
    request_path = tmp_path / "proposal.json"
    request_path.write_text(json.dumps(request))

    original_append = StudioOutboxAppender.append_proposal_event
    monkeypatch.setattr(StudioOutboxAppender, "append_proposal_event",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("append failed")))
    assert main(["proposal", "create", str(bundle), "--mode", "proposal",
                 "--file", str(request_path), "--idempotency-key", "A" * 32,
                 "--format", "json"]) == 0
    created_io = capsys.readouterr()
    assert "proposal outbox reconcile deferred (RuntimeError)" in created_io.err
    assert activate(bundle).repository.pending_outbox()
    monkeypatch.setattr(StudioOutboxAppender, "append_proposal_event", original_append)
    created = json.loads(created_io.out)["data"]
    proposal_id = created["proposal"]["proposal_id"]

    assert main(["proposal", "list", str(bundle), "--format", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["data"]["items"][0]["proposal_id"] == proposal_id
    show_argv = ["proposal", "show", str(bundle), proposal_id,
                 "--revision", "1", "--format", "json"]
    assert main(show_argv) == 0
    shown = json.loads(capsys.readouterr().out)["data"]
    assert main(["proposal", "decide", str(bundle), proposal_id,
                 "--revision", "1", "--op", op_id, "--decision", "accepted",
                 "--read-model-token", shown["read_model_token"],
                 "--idempotency-key", "B" * 32, "--format", "json"]) == 0
    decided = json.loads(capsys.readouterr().out)["data"]
    assert main(show_argv) == 0
    ready = json.loads(capsys.readouterr().out)["data"]
    assert ready["state"] == "ready" and decided["read_model_token"] == ready["read_model_token"]
    assert main(["proposal", "withdraw", str(bundle), proposal_id,
                 "--revision", "1", "--read-model-token", ready["read_model_token"],
                 "--reason", "No longer needed", "--idempotency-key", "C" * 32,
                 "--format", "json"]) == 0
    withdrawn = json.loads(capsys.readouterr().out)["data"]
    assert withdrawn["state"] == "withdrawn"

    legacy_path = bundle / ".okf-loom/session/proposals.jsonl"
    legacy_path.write_text(json.dumps({
        "id": "legacy-7", "summary": "Old suggestion", "author": "agent",
        "created_at": "2026-07-15T00:00:00.000Z",
    }, separators=(",", ":")) + "\n")
    legacy_path.chmod(0o600)
    legacy_before = legacy_path.read_bytes()
    assert main(["proposal", "create", str(bundle), "--mode", "proposal",
                 "--file", str(request_path), "--idempotency-key", "Z" * 32,
                 "--regenerate-legacy-id", "missing",
                 "--legacy-source-generation", "0", "--format", "json"]) == 5
    stale = json.loads(capsys.readouterr().out)
    assert stale["error"]["code"] == "legacy_identity_conflict"
    assert legacy_path.read_bytes() == legacy_before
    assert main(["proposal", "create", str(bundle), "--mode", "proposal",
                 "--file", str(request_path), "--idempotency-key", "D" * 32,
                 "--regenerate-legacy-id", "legacy-7",
                 "--legacy-source-generation", "0", "--format", "json"]) == 0
    regenerated = json.loads(capsys.readouterr().out)["data"]
    assert {"kind": "external", "value":
            '{"legacy_identity":{"legacy_id":"legacy-7","source_generation":0}}'} \
        in regenerated["proposal"]["provenance"]
    assert legacy_path.read_bytes() == legacy_before


def test_cli_apply_undo_success_and_replay_use_real_service(
        tmp_path, capsys) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    private_session_root(bundle)
    (bundle / "orders.md").write_text(
        "---\ntype: Table\ntitle: Orders\ndescription: Orders table.\n"
        "tags: [commerce]\ntimestamp: 2026-07-15T00:00:00Z\nresource: /orders.md\n"
        "---\n\n# Orders\n")
    (bundle / "orders.md").chmod(0o600)
    captured = ProductionCapturePort(bundle).capture(
        captured_at="2026-07-15T00:00:00.000Z", proposal_capability_active=True)
    revision = captured.snapshot.concepts[0].revision
    op_id = "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
    request = {"schema": 1, "mode": "proposal", "author": "agent",
        "summary": "Tag orders", "rationale": "Review metadata.",
        "expected_snapshot": captured.snapshot.to_mapping(),
        "operations": [{"schema": 1, "op_id": op_id, "kind": "set_frontmatter",
            "target": "orders", "args": {"key": "tags", "value": ["orders"]},
            "reads": ["orders"], "writes": ["orders"],
            "expected_revisions": {"orders": revision}, "depends_on": [],
            "extensions": {}}], "provenance": [], "extensions": {}}
    request_path = tmp_path / "proposal.json"
    request_path.write_text(json.dumps(request))

    assert main(["proposal", "create", str(bundle), "--mode", "proposal",
                  "--file", str(request_path), "--idempotency-key", "A" * 32]) == 0
    proposal_id = json.loads(capsys.readouterr().out)["data"]["proposal"]["proposal_id"]
    assert main(["proposal", "show", str(bundle), proposal_id, "--revision", "1"]) == 0
    shown = json.loads(capsys.readouterr().out)["data"]
    assert main(["proposal", "decide", str(bundle), proposal_id, "--revision", "1",
                 "--op", op_id, "--decision", "accepted",
                 "--read-model-token", shown["read_model_token"],
                 "--idempotency-key", "B" * 32]) == 0
    json.loads(capsys.readouterr().out)
    assert main(["proposal", "show", str(bundle), proposal_id, "--revision", "1"]) == 0
    ready = json.loads(capsys.readouterr().out)["data"]
    apply_argv = ["proposal", "apply", str(bundle), proposal_id, "--revision", "1",
                  "--actor", "local-user", "--snapshot-digest",
                  captured.snapshot.snapshot_digest,
                  "--validation-context-digest",
                  captured.snapshot.validation_context_digest,
                  "--decision-set-token", ready["decision_set_token"],
                  "--read-model-token", ready["read_model_token"],
                  "--idempotency-key", "C" * 32]
    assert main(apply_argv) == 0
    applied = json.loads(capsys.readouterr().out)["data"]
    assert applied["status"] == "applied" and applied["replayed"] is False
    assert main(apply_argv) == 0
    assert json.loads(capsys.readouterr().out)["data"]["replayed"] is True

    undo_argv = ["proposal", "undo", str(bundle), proposal_id, "--revision", "1",
                 "--actor", "local-user", "--transaction-id", applied["transaction_id"],
                 "--undo-group-id", applied["undo_group_id"], "--read-model-token",
                 ready["read_model_token"], "--idempotency-key", "D" * 32]
    assert main(undo_argv) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "undone"
    assert main(undo_argv) == 0
    assert json.loads(capsys.readouterr().out)["data"]["replayed"] is True
