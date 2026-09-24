"""CLI outcome and HTTP apply admission contracts, without a running server."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from okf_loom.studio import Studio
from okf_loom.model import Bundle
from okf_loom.server import OKFWikiHandler


CLI = Path(__file__).resolve().parents[1] / "scripts" / "okf-loom"


@pytest.fixture
def bundle(tmp_path: Path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.md").write_text("---\nokf_version: '0.1'\n---\n# Bundle\n")
    (root / "log.md").write_text("# Log\n")
    for name in ("note", "protected"):
        (root / f"{name}.md").write_text(
            "---\ntype: Note\ntitle: Original\n---\n# Note\n\nrepeat repeat\n"
        )
    (root / "okf-loom.config.yaml").write_text(
        "studio:\n  constraints:\n    forbidden_paths: [protected]\n"
    )
    with Studio.for_configured_bundle(root) as studio:
        studio.ensure_session()
        yield root, studio


def run_cli(*args: str):
    result = subprocess.run(
        [str(CLI), *args], capture_output=True, text=True, timeout=15,
    )
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


@pytest.mark.parametrize("command", ["set-frontmatter", "update"])
def test_cli_constraint_denial_is_failure_without_writes(bundle, tmp_path, command):
    root, studio = bundle
    before = (root / "protected.md").read_bytes()
    if command == "update":
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"ops": [{
            "kind": "set_frontmatter", "target": "protected",
            "args": {"key": "title", "value": "Changed"},
        }]}))
        args = ["update", str(root), "--plan", str(plan)]
    else:
        args = ["set-frontmatter", "--bundle", str(root), "--id", "protected",
                "--key", "title", "--value", "Changed"]

    rc, result = run_cli(*args, "--format", "json")

    assert result["applied"] == 0
    assert result["results"][0][1]["reason"] == (
        "studio_blocked:forbidden_paths constraint blocked 'protected'"
    )
    assert (root / "protected.md").read_bytes() == before
    assert not list(studio.history_dir.rglob("*.md"))
    assert rc == 1


def apply_http(root, studio, kind, args, **extra):
    """Exercise the production post-admission handler; no socket or service."""
    handler = object.__new__(OKFWikiHandler)
    handler.server = SimpleNamespace(
        bundle=Bundle.load(root), studio=studio, config={}, _palette={},
    )
    responses = []
    handler._send_json = lambda status, payload: responses.append((status, payload))
    handler._handle_apply({"kind": kind, "target": "note", "args": args, **extra})
    assert len(responses) == 1
    return responses[0]


@pytest.mark.parametrize("kind,args,key", [
    ("replace_text", {"old": "repeat", "new": "changed"}, "all"),
    ("update_section", {"heading": "Missing", "body": "changed"}, "create_if_missing"),
    ("add_link", {"target_concept_id": "missing"}, "allow_forward_reference"),
])
@pytest.mark.parametrize("value", ["false", "true", "", " ", 0, 1, 0.0, 1.0, None, [], {}])
def test_http_optional_boolean_rejected_before_mutation(bundle, kind, args, key, value):
    root, studio = bundle
    before = (root / "note.md").read_bytes()

    status, payload = apply_http(root, studio, kind, {**args, key: value})

    assert (root / "note.md").read_bytes() == before
    assert not list(studio.history_dir.rglob("*.md"))
    assert (status, payload) == (400, {
        "ok": False, "error": f"bad_type:{kind}.{key} must be bool",
    })


@pytest.mark.parametrize("kind,args,key,reason", [
    ("replace_text", {"old": "repeat", "new": "changed"}, "all", "text_ambiguous:2"),
    ("update_section", {"heading": "Missing", "body": "changed"},
     "create_if_missing", "section_not_found"),
    ("add_link", {"target_concept_id": "missing"},
     "allow_forward_reference", "target_concept_not_found"),
])
@pytest.mark.parametrize("value", [True, False, "omitted"])
def test_http_optional_boolean_valid_and_omitted_semantics(bundle, kind, args, key, reason, value):
    root, studio = bundle
    before = (root / "note.md").read_bytes()
    supplied = args if value == "omitted" else {**args, key: value}

    status, payload = apply_http(root, studio, kind, supplied)

    assert status == 200 and payload["ok"] is True
    assert payload["applied"] is (value is True)
    after = (root / "note.md").read_bytes()
    if value is True:
        assert after != before
        if kind == "replace_text":
            assert b"changed changed" in after and b"repeat" not in after
        elif kind == "update_section":
            assert b"## Missing\n\nchanged" in after
        else:
            assert b"missing.md" in after
    else:
        assert after == before
        assert payload["result"]["results"][0][1]["reason"] == reason


@pytest.mark.parametrize("kind,args,key,value,type_name", [
    ("add_link", {"target_concept_id": "protected"}, "label", 1, "str"),
    ("add_link", {"target_concept_id": "protected"}, "anchor_text", False, "str"),
    ("add_link", {"target_concept_id": "protected"}, "section", [], "str or NoneType"),
    ("add_relation", {"target_concept_id": "protected", "relation_type": "uses"},
     "detail", None, "str"),
    ("add_entity", {"label": "Example"}, "kind", {}, "str"),
    ("add_entity", {"label": "Example"}, "entity_id", 1, "str"),
    ("add_entity", {"label": "Example"}, "aliases", "alias", "list"),
    ("update_section", {"heading": "Note", "body": "changed"}, "mode", True, "str"),
])
def test_http_other_optional_fields_follow_schema(bundle, kind, args, key, value, type_name):
    root, studio = bundle
    before = (root / "note.md").read_bytes()
    assert apply_http(root, studio, kind, {**args, key: value}) == (400, {
        "ok": False, "error": f"bad_type:{kind}.{key} must be {type_name}",
    })
    assert (root / "note.md").read_bytes() == before


@pytest.mark.parametrize("section", [None, "Links"])
def test_http_optional_nullable_section_is_accepted(bundle, section):
    root, studio = bundle
    status, payload = apply_http(root, studio, "add_link", {
        "target_concept_id": "protected", "section": section,
    })
    assert status == 200 and payload["applied"] is True
    assert b"protected.md" in (root / "note.md").read_bytes()


@pytest.mark.parametrize("value", [False, True, 0, 1, [], {}, None])
def test_http_any_value_keeps_non_null_json_semantics(bundle, value):
    root, studio = bundle
    status, payload = apply_http(root, studio, "set_frontmatter", {
        "key": "custom", "value": value,
    })
    if value is None:
        assert (status, payload) == (400, {
            "ok": False, "error": "bad_type:set_frontmatter.value must not be null",
        })
    else:
        assert status == 200 and payload["applied"] is True
        stored = Bundle.load(root).concepts[("note",)].frontmatter["custom"]
        assert type(stored) is type(value) and stored == value


@pytest.mark.parametrize("args,error", [
    ({"new": "changed"}, "missing_arg:replace_text.old"),
    ({"old": 1, "new": "changed"}, "bad_type:replace_text.old must be str"),
    ({"old": "repeat", "new": "changed", "unexpected": True},
     "unknown_arg:replace_text.unexpected"),
])
def test_http_required_and_unknown_argument_contracts_unchanged(bundle, args, error):
    root, studio = bundle
    before = (root / "note.md").read_bytes()
    assert apply_http(root, studio, "replace_text", args) == (400, {"ok": False, "error": error})
    assert (root / "note.md").read_bytes() == before


def test_cli_success_then_genuine_noop_in_constrained_studio(bundle):
    root, _studio = bundle
    args = ["set-frontmatter", "--bundle", str(root), "--id", "note",
            "--key", "title", "--value", "Changed", "--format", "json"]
    rc, result = run_cli(*args)
    assert rc == 0 and result["applied"] == 1
    before = (root / "note.md").read_bytes()
    rc, result = run_cli(*args)
    assert rc == 0 and result["applied"] == 0
    assert result["results"][0][1]["reason"] == "same_value"
    assert (root / "note.md").read_bytes() == before


@pytest.mark.parametrize("target,expected_rc,applied,reason", [
    ("missing", 0, 1, "concept_not_found"),
    ("protected", 1, 0, "studio_blocked:forbidden_paths constraint blocked 'protected'"),
])
def test_cli_update_partial_success_and_blocked_rollback(bundle, tmp_path, target, expected_rc, applied, reason):
    root, _studio = bundle
    before = (root / "note.md").read_bytes()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"ops": [
        {"kind": "set_frontmatter", "target": cid,
         "args": {"key": "title", "value": "Changed"}}
        for cid in ("note", target)
    ]}))
    rc, result = run_cli("update", str(root), "--plan", str(plan), "--format", "json")
    assert rc == expected_rc and result["applied"] == applied
    assert result["results"][1][1]["reason"] == reason
    if applied:
        assert b"title: Changed" in (root / "note.md").read_bytes()
    else:
        assert (root / "note.md").read_bytes() == before
        assert result["results"][0][1]["reason"] == "transaction_rolled_back"


def test_cli_conflict_preserves_external_edit(bundle, monkeypatch, capsys):
    from okf_loom import cli
    from okf_loom.studio import rev_of

    root, _studio = bundle
    original_load = cli._load_bundle
    before = (root / "note.md").read_bytes()
    concurrent = before + b"\nConcurrent edit\n"

    def load_then_external_edit(path):
        loaded = original_load(path)
        (root / "note.md").write_bytes(concurrent)
        return loaded

    # Deterministic external filesystem race after CLI read, before real CAS.
    monkeypatch.setattr(cli, "_load_bundle", load_then_external_edit)
    rc = cli.main(["set-frontmatter", "--bundle", str(root), "--id", "note",
                   "--key", "title", "--value", "Changed", "--format", "json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert rc == 1 and result["applied"] == 0
    assert result["results"][0][1]["reason"] == "rev_conflict"
    assert result["results"][0][1]["conflict"] == {
        "expected_rev": rev_of(before), "current_rev": rev_of(concurrent),
    }
    assert (root / "note.md").read_bytes() == concurrent
